# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CAT (Closed-loop Adversarial Training) is a research project for safe end-to-end autonomous driving using adversarial scenario generation. The system dynamically creates safety-critical traffic scenarios to train more robust driving policies using the Waymo Open Motion Dataset.

## Development Commands

### Environment Setup
```bash
conda create -n cat python=3.9
conda activate cat
pip install -r requirements.txt
```

### Build and Compilation
```bash
# Compile Cython extensions for performance optimization
python setup.py build_ext --inplace
```

### Main Execution Commands
```bash
# Run adversarial scenario generation and visualization
python cat_advgen.py

# Train CAT policy with adversarial scenarios
python cat_RLtrain.py --mode cat --seed 0

# Generate learning curve plots
./scripts/plot.sh
```

### Data Processing Commands
```bash
# Convert Waymo data to MetaDrive format
python scripts/convert_WOMD_to_MD.py

# Filter scenarios (9.1s, 1 ego + 1 opponent + n vehicles)
python scripts/select_cases.py

# Visualization and analysis
python scripts/plot.py
```

## Architecture Overview

### Core Components

1. **Adversarial Generation** (`advgen/`):
   - `adv_generator.py`: Neural network-based adversarial scenario generator
   - `modeling/vectornet.py`: VectorNet architecture for trajectory prediction
   - `modeling/decoder.py`: Multi-modal trajectory decoders
   - `pretrained/densetnt.bin`: Pre-trained DenseTNT traffic behavior model

2. **Simulation Environment** (`metadrive/`):
   - Modified MetaDrive simulator with Waymo integration
   - `envs/real_data_envs/waymo_env.py`: Waymo dataset environment
   - `policy/replay_policy.py`: Policy implementations and replay functionality

3. **Reinforcement Learning** (`saferl_algo/`):
   - `TD3.py`: Twin Delayed DDPG implementation for safety-aware RL
   - Specialized for closed-loop adversarial training

### Data Flow
1. Waymo scenarios → MetaDrive format conversion
2. Scenario filtering and preprocessing
3. Adversarial scenario generation using neural networks
4. Policy training with TD3 in adversarial environments
5. Safety evaluation (crash rates, route completion)

## Key Files and Entry Points

- **`cat_advgen.py`**: Main demonstration of adversarial scenario generation
- **`cat_RLtrain.py`**: CAT training pipeline with TD3
- **`advgen/adv_generator.py`**: Core adversarial generation logic
- **`setup.py`**: Cython compilation configuration
- **`raw_scenes_500/`**: Preprocessed Waymo scenarios (500 scenes)

## Dependencies and Requirements

### Critical Dependencies
- Python 3.9 (Cython compatibility)
- PyTorch (GPU support recommended)
- Gym 0.22.0 (specific version requirement)
- Panda3D (3D simulation rendering)
- Waymo Open Dataset tools

### Data Requirements
- DenseTNT pretrained model: `advgen/pretrained/densetnt.bin`
- Waymo WOMD v1.1 validation/testing_interactive data
- 500 preprocessed scenarios in `raw_scenes_500/`

## Performance Considerations

### Cython Optimization
- `advgen/utils_cython.pyx` contains performance-critical computations
- Must be compiled with `python setup.py build_ext --inplace` after changes
- Generated `.so` files are platform-specific

### GPU Acceleration
- CUDA support available for PyTorch operations
- Automatic CPU fallback implemented
- GPU recommended for training but not required for inference

## Development Notes

### Neural Network Architecture
- VectorNet: Graph-based trajectory prediction with spatial relations
- Multi-modal decoders for trajectory generation
- Pre-trained DenseTNT provides traffic behavior priors

### Simulation Features
- Real-time 3D rendering with top-down visualization
- Multi-agent traffic scenarios with physics simulation
- Comprehensive sensor simulation (LiDAR, cameras)

### Safety Metrics
- Attack success rate measurement
- Crash detection and route completion tracking
- Adversarial effectiveness evaluation