"""Run SAR-only detection, retaining slope, SLIC, and Bonferroni."""

from ablation.pipeline import main

if __name__ == "__main__":
    main("sar_only")
