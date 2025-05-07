<div align="center">

# SkyRL-v0: Train Real-World Long-Horizon Agents via Reinforcement Learning

[![Github](https://img.shields.io/badge/SkyRL-000000?style=for-the-badge&logo=github&logoColor=000&logoColor=white)](https://github.com/NovaSky-AI/SkyRL) [![Twitter](https://img.shields.io/badge/NovaSky-white?style=for-the-badge&logo=X&logoColor=000&color=000&labelColor=white)](https://x.com/NovaSkyAI) [![Hugging Face Collection](https://img.shields.io/badge/NovaSky-fcd022?style=for-the-badge&logo=huggingface&logoColor=000&labelColor)](https://huggingface.co/NovaSky-AI) [![Discord](https://img.shields.io/badge/NovaSky-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/RBAjeWSA)


<div align="center" style="font-family: Arial, sans-serif;">
  <p>
    <a href="#news" style="text-decoration: none; font-weight: bold;">News</a> •
    <a href="#links" style="text-decoration: none; font-weight: bold;">Links</a> •
    <a href="#getting-started" style="text-decoration: none; font-weight: bold;">Getting Started</a> •
    <a href="#evaluation" style="text-decoration: none; font-weight: bold;">Evaluation</a> •
    <a href="#citation" style="text-decoration: none; font-weight: bold;">Citation</a> •
    <a href="#acknowledgement" style="text-decoration: none; font-weight: bold;">Acknowledgement</a> 
  </p>
</div>

</div>


# News
- **[2025/05/06]** 🎉 We released SkyRL-v0: the first open-source online RL training framework for multi-turn tool use LLMs, optimized for long-horizon, real-environment tasks like SWE-Bench!


# Links

- 📜 [SkyRL-v0 Blog Post](https://novasky-ai.github.io/posts/sky-gym-v0/)


# Getting Started
This repository contains training code for the `SkyRL-v0` release. Our implementation is a fork of [VeRL](https://github.com/volcengine/verl).

## Installation

The only pre-requisite is having `uv` [installed](https://docs.astral.sh/uv/getting-started/installation) on your system. We use the `uv` + `ray` integration to easily manage dependencies in multi-node training. 

### Clone SkyRL-OpenHands

We use [SkyRL-OpenHands](https://github.com/NovaSky-AI/SkyRL-OpenHands) to be able to connect to our remote runtime server. Clone the repository and place it in the git root:

```bash 
git clone https://github.com/NovaSky-AI/SkyRL-OpenHands
```

### Installation dry run

You can dry run your installation with the following command: 

```bash
uv run --isolated --frozen pip show torch
```

NOTE: With a CPU head node, you might encounter installation issues with `torch-memory-saver`. To fix this, you need to install CUDA and make sure your CUDA libraries are linked in `/usr/lib`. For example, 

```bash
sudo ln -s /usr/local/cuda-12.4/compat/libcuda.so /usr/lib/libcuda.so
sudo ln -s /usr/local/cuda-12.4/compat/libcuda.so.1 /usr/lib/libcuda.so.1
```

## Scripts for reproduction

For reproducing our results for SkyRL-Agent-14B-v0, SkyRL-Agent-8B-v0, and SkyRL-Agent-7B-v0 you can refer to [examples/sky](./examples/sky/README.md).

# SWE-Bench-Verified Evaluation Results

| Model              | Base                 | Base Performance | Performance | Training Time |
|--------------------|----------------------|------------------|-------------|---------------|
| SkyRL-Agent-7B-v0  | OpenHands-7B-Agent   | 11%              | 14.6%       | 16hrs 8xH100  |
| SkyRL-Agent-8B-v0  | Qwen3-8B no thinking | 3.6%             | 9.4%        | 27hrs 8xH200  |
| SkyRL-Agent-14B-v0 | Qwen3-14B thinking   | 18%              | 21.6%       | 20hrs 8xH200  |


# Acknowledgement
This work is done at [Berkeley Sky Computing Lab](https://sky.cs.berkeley.edu/), with the amazing compute support from [Lambda Labs](https://lambdalabs.com/service/gpu-cloud?srsltid=AfmBOop5FnmEFTkavVtdZDsLWvHWNg6peXtat-OXJ9MW5GMNsk756PE5), [Anyscale](https://www.anyscale.com/), and [Databricks](https://www.databricks.com/).