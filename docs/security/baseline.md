# Security Automation Baseline

Ruff security rules and Bandit provide Python SAST, pip-audit checks locked dependencies, Gitleaks scans repository history, and Trivy scans the built image in CI. Critical and High findings block unless the execution policy's documented time-bounded High-risk acceptance process is satisfied; Medium findings are reviewed. Scanner outages fail closed for mandatory gates. Suppressions must be local, justified, owned and expiring.
