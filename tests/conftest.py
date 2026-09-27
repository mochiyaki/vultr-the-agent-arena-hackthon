import os

# Tests exercise the agent loop with the dev driver; the Docker driver is covered by
# deploy-time smoke tests on the Vultr VM (see README "Verify containment").
os.environ.setdefault("SANDBOX_DRIVER", "unsafe_local")
os.environ.setdefault("ALLOW_UNSAFE_LOCAL_SANDBOX", "1")
os.environ.setdefault("VULTR_INFERENCE_API_KEY", "test-key")
