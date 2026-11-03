"""Run fused detection with uncorrected alpha=0.05 significance tests."""

from ablation.pipeline import main

if __name__ == "__main__":
    main("no_bonferroni")
