"""
Run with:
uv run --isolated --extra dev --extra vllm --extra mcore -- pytest tests/gpu/test_megatron_worker.py
"""

import ray
import pytest
import hydra
from omegaconf import DictConfig
import torch
import asyncio
from transformers import AutoModelForCausalLM, AutoTokenizer

from tests.gpu.utils import (
    init_worker_with_type,
    ray_init_for_tests,
    get_rank_0_memory,
    init_inference_engines,
    run_inference,
    get_test_prompts,
)

from skyrl_train.workers.worker_utils import BatchIterator
from skyrl_train.utils.utils import print_mem, validate_cfg
from skyrl_train.entrypoints.main_base import config_dir
from skyrl_train.distributed.dispatch import concatenate_outputs_after_mesh_dispatch
from skyrl_train.utils.torch_utils import logprobs_from_logits
from skyrl_train.training_batch import TrainingInputBatch
from skyrl_train.inference_engines.utils import get_sampling_params_for_backend


MODEL_NAME = "Qwen/Qwen3-0.6B"


def get_test_actor_config() -> DictConfig:
    with hydra.initialize_config_dir(config_dir=config_dir):
        cfg = hydra.compose(config_name="ppo_base_config")

    cfg.trainer.policy.model.path = MODEL_NAME
    cfg.trainer.micro_forward_batch_size_per_gpu = 2
    cfg.trainer.micro_train_batch_size_per_gpu = 2
    cfg.trainer.use_sample_packing = False

    validate_cfg(cfg)

    return cfg


def get_test_training_batch() -> TrainingInputBatch:
    """
    Returns a test training batch with padded seqs and attention masks

    Gives a batch of 4 sequences with variable amounts of left padding, and variable response lengths/amounts of right padding
    Attention masks are 1 for non-padding tokens, 0 for padding tokens
    The rest of the fields are filled with dummy data
    """
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

    sentences = [
        "<|im_start|>system\nYou are Qwen, created by Alibaba Cloud. You are a helpful assistant.",
        "<|im_start|>user\nThe selling price of a bicycle that had sold $220 last year was increased by 15",
        "What is the new price? Let's think step by step and output the final answer after `####`.<|im_end|>\n",
        "<|im_start|>assistant\nTo find the new price of the bicycle after the increase,",
    ]

    sequences = [tokenizer.encode(sentence) for sentence in sentences]
    attention_masks = [[1] * len(seq) for seq in sequences]
    batch_size = len(sequences)
    num_actions = 10

    pad_token_id = tokenizer.pad_token_id

    # pad to length of longest sequence (25)
    pad_before_after = [(4, 2), (0, 1), (1, 1), (6, 4)]
    for i, (pad_before, pad_after) in enumerate(pad_before_after):
        sequences[i] = [pad_token_id] * pad_before + sequences[i] + [pad_token_id] * pad_after
        attention_masks[i] = [0] * pad_before + attention_masks[i] + [0] * pad_after

    attention_masks = torch.tensor(attention_masks)
    sequences = torch.tensor(sequences)

    data = TrainingInputBatch(
        {
            "sequences": sequences,
            "attention_mask": attention_masks,
            "action_log_probs": torch.tensor([[0.1] * num_actions] * batch_size),
            "base_action_log_probs": torch.tensor([[0.2] * num_actions] * batch_size),
            "rollout_logprobs": torch.tensor([[0.11] * num_actions] * batch_size),
            "values": torch.tensor([[0.1] * num_actions] * batch_size),
            "returns": torch.tensor([[0.1] * num_actions] * batch_size),
            "advantages": torch.tensor([[0.5] * num_actions] * batch_size),
            "loss_mask": torch.tensor([[1] * num_actions] * batch_size),
            "response_mask": torch.tensor([[1] * num_actions] * batch_size),
        }
    )
    data.metadata = {"response_length": num_actions}
    return data


@pytest.fixture
def cfg() -> DictConfig:
    return get_test_actor_config()


def test_megatron_policy_weight_sync(cfg):
    """
    Test that we can sync weights between policy and inference for megatron then run inference
    """
    try:
        cfg = get_test_actor_config()
        cfg.trainer.placement.colocate_all = True
        cfg.generator.weight_sync_backend = "nccl"
        cfg.trainer.strategy = "megatron"
        cfg.generator.backend = "vllm"
        cfg.generator.inference_engine_tensor_parallel_size = 4

        # set tp and pp to 2 to check that gather for weight sync works correctly
        cfg.trainer.policy.megatron_config.tensor_model_parallel_size = 2
        cfg.trainer.policy.megatron_config.pipeline_model_parallel_size = 2

        # If colocate is True, this will load the engine, sleep, and wake up the engine
        client, pg = init_inference_engines(
            model=MODEL_NAME,
            cfg=cfg,
            use_local=True,
            async_engine=cfg.generator.async_engine,
            tp_size=cfg.generator.inference_engine_tensor_parallel_size,
            colocate_all=cfg.trainer.placement.colocate_all,
            backend="vllm",
        )

        policy = init_worker_with_type(
            "policy",
            shared_pg=pg,
            colocate_all=cfg.trainer.placement.colocate_all,
            num_gpus_per_node=cfg.generator.inference_engine_tensor_parallel_size,
            cfg=cfg,
        )
        ray.get(policy.async_run_ray_method("pass_through", "init_weight_sync_state", client))
        asyncio.run(client.reset_prefix_cache())
        ray.get(policy.async_run_ray_method("pass_through", "broadcast_to_inference_engines", client))
        sampling_params = get_sampling_params_for_backend(cfg.generator.backend, cfg.generator.sampling_params)
        outputs = asyncio.run(run_inference(client, get_test_prompts(MODEL_NAME), sampling_params))

        print(f"Example output: {outputs['responses'][0]}, {outputs['stop_reasons'][0]}")
    finally:
        ray.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_type", "tp", "pp", "gpus_per_node"),
    [
        ("policy", 2, 1, 2),
        ("ref", 2, 1, 2),  # ref has same forward pass as policy - just duplicate one test to test setup
        ("policy", 1, 2, 2),
        ("policy", 2, 2, 4),
    ],
    ids=["tp2_pp1_policy", "tp2_pp1_ref", "tp1_pp2_policy", "tp2_pp2_policy"],
)
async def test_megatron_forward(cfg, ray_init_fixture, worker_type, tp, pp, gpus_per_node):
    """
    Test that the Megatron forward pass is numerically equivalent to just running a huggingface model forward.
    """
    #### Megatron forward pass ####
    cfg.trainer.strategy = "megatron"
    cfg.trainer.placement.policy_num_gpus_per_node = gpus_per_node
    cfg.trainer.policy.megatron_config.tensor_model_parallel_size = tp
    cfg.trainer.policy.megatron_config.pipeline_model_parallel_size = pp
    batch = get_test_training_batch()

    actor_group = init_worker_with_type(
        worker_type,
        shared_pg=None,
        colocate_all=False,
        num_gpus_per_node=cfg.trainer.placement.policy_num_gpus_per_node,
        cfg=cfg,
    )

    action_log_probs_refs = actor_group.async_run_ray_method("mesh", "forward", data=batch)
    all_rank_action_log_probs = ray.get(action_log_probs_refs)
    action_log_probs_megatron = concatenate_outputs_after_mesh_dispatch(
        actor_group.actor_infos, all_rank_action_log_probs
    )["output"]

    ray.shutdown()
    ray_init_for_tests()

    #### Huggingface forward pass ####
    # now run the huggingface model forward
    model = AutoModelForCausalLM.from_pretrained(MODEL_NAME, torch_dtype=torch.bfloat16)
    model.eval()
    model.to("cuda")
    sequences_fwd = batch["sequences"]
    attention_mask = batch["attention_mask"]
    num_actions = batch.metadata["response_length"]

    position_ids = attention_mask.long().cumsum(-1) - 1
    position_ids.masked_fill_(attention_mask == 0, 1)

    sequences_rolled = torch.roll(sequences_fwd, shifts=-1, dims=1).to("cuda")

    sequences_fwd, attention_mask, position_ids = (
        sequences_fwd.to("cuda"),
        attention_mask.to("cuda"),
        position_ids.to("cuda"),
    )
    with torch.no_grad(), torch.autocast(dtype=torch.bfloat16, device_type="cuda"):
        output = model(sequences_fwd, attention_mask=attention_mask, position_ids=position_ids)
        log_probs = logprobs_from_logits(output["logits"], sequences_rolled)
        action_log_probs = log_probs[:, -num_actions - 1 : -1].to("cpu").detach()

    #### Compare results ####
    # compare just non-padding tokens
    attention_mask = attention_mask.to("cpu").detach()

    # Create response mask: 1 for valid response tokens, 0 for padding
    response_mask = attention_mask[:, -num_actions:].bool()

    # Only compare valid (non-padding) response tokens
    action_log_probs_masked = action_log_probs[response_mask]
    action_log_probs_megatron_masked = action_log_probs_megatron[response_mask]

    print(f"Comparing {action_log_probs_masked.numel()} valid response tokens")
    print(f"HF sample: {action_log_probs_masked[:5]}")
    print(f"Megatron sample: {action_log_probs_megatron_masked[:5]}")

    # max diff
    max_diff = torch.max(torch.abs(action_log_probs_masked - action_log_probs_megatron_masked))
    print(f"Max diff: {max_diff}")

    # average diff
    avg_diff = torch.mean(torch.abs(action_log_probs_masked - action_log_probs_megatron_masked))
    print(f"Avg diff: {avg_diff}")
    assert avg_diff < 5e-2, f"Avg diff {avg_diff} is too large"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_type", "tp", "pp", "gpus_per_node"),
    [
        ("policy", 2, 2, 4),
    ],
)
async def test_megatron_training_step(cfg, ray_init_fixture, worker_type, tp, pp, gpus_per_node):
    """
    Full test: initialize actor group, send dummy experience to training_step, validate output.
    """

    batch = get_test_training_batch()

    cfg.trainer.strategy = "megatron"
    cfg.trainer.placement.policy_num_gpus_per_node = gpus_per_node
    cfg.trainer.policy.megatron_config.tensor_model_parallel_size = tp
    cfg.trainer.policy.megatron_config.pipeline_model_parallel_size = pp

    # set batch sizes correctly
    cfg.trainer.train_batch_size = 4
    cfg.trainer.policy_mini_batch_size = 2
    cfg.generator.n_samples_per_prompt = 1
    cfg.trainer.micro_train_batch_size_per_gpu = 1

    actor_group = init_worker_with_type(
        "policy",
        shared_pg=None,
        colocate_all=False,
        num_gpus_per_node=cfg.trainer.placement.policy_num_gpus_per_node,
        cfg=cfg,
    )

    # call ppo_train with a batch of size 4 per gpu
    batch.metadata["global_step"] = 0
    results_megatron = ray.get(actor_group.async_run_ray_method("pass_through", "ppo_train", batch))
    results_megatron = [results_megatron[i].metadata["train_status"] for i in range(len(results_megatron))]

    memory = ray.get(actor_group.async_run_ray_method("pass_through", "get_cuda_memory"))
    memory = memory[0]
    print_mem("memory after training step", memory)

    for result in results_megatron:
        assert isinstance(result, dict), "Result should be a dictionary of training stats"
        assert "policy_loss" in result
        assert "policy_lr" in result
        assert "ppo_clip_ratio" in result
        assert "policy_entropy" in result
        for k, v in result.items():
            assert isinstance(v, (int, float)), f"{k} should be an int or float"

    ray.shutdown()
    ray_init_for_tests()

    # manually run the same batch with FSDP via training step
    experience = BatchIterator.batch_to_experience(batch)
    global_step, local_step, accumulation_steps = 0, 0, 2

    cfg.trainer.strategy = "fsdp"
    actor_group = init_worker_with_type(
        "policy",
        shared_pg=None,
        colocate_all=False,
        num_gpus_per_node=cfg.trainer.placement.policy_num_gpus_per_node,
        cfg=cfg,
    )

    results_fsdp = ray.get(
        actor_group.async_run_ray_method(
            "pass_through", "training_step", experience, global_step, local_step, accumulation_steps
        )
    )

    print("megatron results: ", results_megatron)
    print("fsdp results: ", results_fsdp)

    keys_to_compare = ["policy_loss", "policy_lr", "ppo_clip_ratio", "policy_entropy", "policy_kl"]
    for i, result in enumerate(results_fsdp):
        for k in keys_to_compare:
            if k == "policy_entropy":
                # TODO: make entropy calculation only apply to non-padding tokens for all backends
                # because the logits for padding tokens are all 0 for the non-sample packing case in megatron
                # the entropy calculation is different (fsdp has random logits for padding tokens)
                continue
            assert isinstance(result[k], (int, float)), f"{k} should be an int or float"
            assert abs(result[k] - results_megatron[i][k]) < 1.5e-1, f"diff in {k} is too large!"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_type", "tp", "pp", "gpus_per_node"),
    [
        ("policy", 2, 2, 4),
    ],
)
async def test_megatron_dp(cfg, ray_init_fixture, worker_type, tp, pp, gpus_per_node):
    """
    Full test: initialize actor group, send dummy experience to training_step, validate output.
    """

    batch = get_test_training_batch()

    cfg.trainer.strategy = "megatron"
    cfg.trainer.placement.policy_num_gpus_per_node = gpus_per_node

    # try tp=2, pp=2 first
    cfg.trainer.policy.megatron_config.tensor_model_parallel_size = tp
    cfg.trainer.policy.megatron_config.pipeline_model_parallel_size = pp

    # set batch sizes correctly
    cfg.trainer.train_batch_size = 64
    cfg.trainer.policy_mini_batch_size = len(batch["sequences"])  # self.policy_mini_batch_size_per_gpu = 2 * 1 / 1 = 2
    cfg.generator.n_samples_per_prompt = 1
    cfg.trainer.micro_train_batch_size_per_gpu = 4

    # set torch profiler config
    cfg.trainer.policy.megatron_config.torch_profiler_config.enable = True
    cfg.trainer.policy.megatron_config.torch_profiler_config.ranks = [0]
    cfg.trainer.policy.megatron_config.torch_profiler_config.save_path = "/home/ray/megatron_prof/tp2_pp2/"

    actor_group = init_worker_with_type(
        "policy",
        shared_pg=None,
        colocate_all=False,
        num_gpus_per_node=cfg.trainer.placement.policy_num_gpus_per_node,
        cfg=cfg,
    )

    # call ppo_train with a batch of size 4 per gpu
    batch.metadata["global_step"] = 0
    results_megatron = ray.get(actor_group.async_run_ray_method("mesh", "ppo_train", batch))
    results_megatron = [results_megatron[i].metadata["train_status"] for i in range(len(results_megatron))]

    memory = ray.get(actor_group.async_run_ray_method("pass_through", "get_cuda_memory"))
    memory = memory[0]
    print_mem("memory after training step", memory)

    for result in results_megatron:
        assert isinstance(result, dict), "Result should be a dictionary of training stats"
        assert "policy_loss" in result
        assert "policy_lr" in result
        assert "ppo_clip_ratio" in result
        assert "policy_entropy" in result
        for k, v in result.items():
            assert isinstance(v, (int, float)), f"{k} should be an int or float"

    ray.shutdown()
    ray_init_for_tests()

    # check the grad norm for the same thing but with pp=1, tp=1, dp=4
    cfg.trainer.policy.megatron_config.tensor_model_parallel_size = 1
    cfg.trainer.policy.megatron_config.pipeline_model_parallel_size = 1

    # set torch profiler config
    cfg.trainer.policy.megatron_config.torch_profiler_config.save_path = "/home/ray/megatron_prof/dp4/"

    # set batch sizes correctly
    cfg.trainer.train_batch_size = 64
    cfg.trainer.policy_mini_batch_size = len(batch["sequences"])  # self.policy_mini_batch_size_per_gpu = 8 * 1 / 4 = 2
    cfg.generator.n_samples_per_prompt = 1
    cfg.trainer.micro_train_batch_size_per_gpu = 4

    actor_group = init_worker_with_type(
        "policy",
        shared_pg=None,
        colocate_all=False,
        num_gpus_per_node=cfg.trainer.placement.policy_num_gpus_per_node,
        cfg=cfg,
    )

    results_megatron_dp = ray.get(actor_group.async_run_ray_method("mesh", "ppo_train", batch))
    results_megatron_dp = [results_megatron_dp[i].metadata["train_status"] for i in range(len(results_megatron_dp))]

    print("megatron results: ", results_megatron)
    print("\n\n")
    print("megatron results dp: ", results_megatron_dp)

    keys_to_compare = ["policy_loss", "policy_lr", "ppo_clip_ratio", "policy_entropy", "policy_kl", "raw_grad_norm"]
    for i, result in enumerate(results_megatron_dp):
        for k in keys_to_compare:
            assert isinstance(result[k], (int, float)), f"{k} should be an int or float"
            assert abs(result[k] - results_megatron[i][k]) < 1.5e-1, f"diff in {k} is too large!"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_type"),
    [
        "policy",
    ],
)
async def test_megatron_offload_memory_and_correctness(cfg, worker_type):
    """
    Test that offloading model memory to cpu lowers memory usage and that correctness
    is maintained after backloading and running a training step.

    steps:
    1. Initialize actor group with the specified worker class.
    2. Offload model to CPU and check memory usage.
    3. Backload model to GPU and check memory usage.
    4. Run a training step with dummy experience (with optimizer step)
    5. Offload model to CPU again and check memory usage.
    6. Backload model to GPU and check memory usage.
    7. Run another training step and ensure output consistency.
    """
    cfg.trainer.strategy = "megatron"
    # 0 learning rate and wd so we can optimizer step to free gradients but still check results are the same
    getattr(cfg.trainer, worker_type).optimizer_config.lr = 0
    getattr(cfg.trainer, worker_type).optimizer_config.weight_decay = 0
    try:
        actor_group = init_worker_with_type(
            worker_type,
            shared_pg=None,
            colocate_all=False,
            num_gpus_per_node=cfg.trainer.placement.policy_num_gpus_per_node,
            cfg=cfg,
        )
        get_rank_0_memory(actor_group, "After init")
        # offload then backload first (no training step)
        actor_group.offload_to_cpu()

        initial_offload_mem = get_rank_0_memory(actor_group, "After initial offload")

        # Backload to GPU
        actor_group.backload_to_gpu()
        get_rank_0_memory(actor_group, "Before training")

        batch = get_test_training_batch()
        results = ray.get(actor_group.async_run_ray_method("pass_through", "ppo_train", batch))

        after_training = get_rank_0_memory(actor_group, "After training")

        # Offload model to CPU
        actor_group.offload_to_cpu()

        after_offload = get_rank_0_memory(actor_group, "After offload")

        # check that allocated memory is similar to initial offload memory
        delta = abs(initial_offload_mem - after_offload)
        assert (
            delta < 4e8  # 400MB (should be close to 0 diff)
        ), f"Memory after training step + offload is not similar to initial offloaded memory: {delta} bytes. Initial offload mem: {initial_offload_mem}, after offload mem: {after_offload} bytes"

        # also check that allocated memory goes down after offloading
        delta_forward = after_training - after_offload
        assert (
            delta_forward > 0
        ), f"Memory after offloading should be less than after forward pass: {delta_forward} bytes"

        # Backload model to GPU
        actor_group.backload_to_gpu()

        get_rank_0_memory(actor_group, "After backload")

        # Run training again and ensure output consistency
        results_backload = ray.get(actor_group.async_run_ray_method("pass_through", "ppo_train", batch))

        for i, result in enumerate(results):
            result_backload = results_backload[i]
            for k, v in result.items():
                assert k in result_backload
                assert v == result_backload[k], f"Results mismatch for {k}: {v} != {result_backload[k]}"

    finally:
        ray.shutdown()  # Clean up Ray resources after the test
