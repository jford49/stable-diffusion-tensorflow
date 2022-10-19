# Stable Diffusion in TensorFlow / Keras

A Keras / Tensorflow implementation of Stable Diffusion. 

The weights were ported from the original implementation.

## Colab Notebooks

The easiest way to try it out is to use one of the Colab notebooks:


- [GPU Colab Img2Img](https://colab.research.google.com/drive/1d7uiLFXLI1Gi4jLs57VUxJr7OsZBc1G9?usp=sharing)


## Installation

### Install as a python package

Install using pip with the git repo:

```bash
pip install git+https://github.com/jford49/stable-diffusion-tensorflow
```

### Installing using the repo

Download the repo, either by downloading the
[zip](https://github.com/jford49/stable-diffusion-tensorflow/archive/refs/heads/master.zip)
file or by cloning the repo with git:

```bash
git clone git@github.com:jford49/stable-diffusion-tensorflow.git
```

## Usage

### Using the Python interface

If you installed the package, you can use it as follows:

```python
from stable_diffusion_tf.stable_diffusion import StableDiffusion
from PIL import Image

generator = StableDiffusion(
    img_height=512,
    img_width=512,
    jit_compile=False,
)

# for image to image :
img = generator.generate(
    "A Halloween bedroom",
    num_steps=50,
    unconditional_guidance_scale=7.5,
    temperature=1,
    batch_size=1,
    input_image="/path/to/img.png"
)

Image.fromarray(img[0]).save("output.png")
```

## References

1) https://github.com/CompVis/stable-diffusion
2) https://github.com/geohot/tinygrad/blob/master/examples/stable_diffusion.py
