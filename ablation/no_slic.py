"""Run fused detection with pixelwise rather than superpixel tests."""

from ablation.pipeline import main

if __name__ == "__main__":
    main("no_slic")
