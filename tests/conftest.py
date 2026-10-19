import os

for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[variable] = "1"

os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"
