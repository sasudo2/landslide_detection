"""Run fused detection without slope masking or slope SLIC features."""

from ablation.pipeline import main

if __name__ == "__main__":
    main("no_slope")
