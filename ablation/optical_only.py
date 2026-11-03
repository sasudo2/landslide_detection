"""Run optical-only detection, retaining slope, SLIC, and Bonferroni."""

from ablation.pipeline import main

if __name__ == "__main__":
    main("optical_only")
