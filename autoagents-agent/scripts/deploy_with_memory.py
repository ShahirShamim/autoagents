#!/usr/bin/env python
"""Deploy the agent to Agent Runtime WITH Memory Bank (context_spec).

`agents-cli deploy` strips `context_spec`, so the deployed runtime never wires
Memory Bank. This redeploys via the SAME `deployment_source` path agents-cli uses
(source_packages + class_methods + entrypoint) plus the SAME live env/secrets,
and ADDS `context_spec` so the new container wires Memory Bank at startup.

Reuses the live deployment's env/secrets so it can't drift from the working config.
Run: `uv run python scripts/deploy_with_memory.py`
"""
import tomllib

import vertexai
from vertexai._genai import _agent_engines_utils as u
from vertexai._genai.types import (
    AgentEngineConfig,
    ManagedTopicEnum,
    MemoryBankCustomizationConfig as CustomizationConfig,
    MemoryBankCustomizationConfigMemoryTopic as MemoryTopic,
    MemoryBankCustomizationConfigMemoryTopicManagedMemoryTopic as ManagedMemoryTopic,
    ReasoningEngineContextSpec,
    ReasoningEngineContextSpecMemoryBankConfig as MemoryBankConfig,
)

from app.agent_runtime_app import agent_runtime  # the deployable AdkApp

PROJECT = "autoagents-500500"
LOCATION = "us-central1"
RES = f"projects/323512451403/locations/{LOCATION}/reasoningEngines/5931933951328256000"
ENTRY_MODULE = "app.agent_runtime_app"
ENTRY_OBJECT = "agent_runtime"

client = vertexai.Client(project=PROJECT, location=LOCATION)
vertexai.init(project=PROJECT, location=LOCATION)

# 1. Reuse the live deployment's env + secrets verbatim (can't drift).
ds = client.agent_engines.get(name=RES).api_resource.spec.deployment_spec
env_vars: dict = {ev.name: ev.value for ev in (ds.env or [])}
for sv in ds.secret_env or []:
    env_vars[sv.name] = {"secret": sv.secret_ref.secret, "version": sv.secret_ref.version}
print("env keys:", sorted(env_vars))

# 2. Requirements from pyproject -> a requirements.txt file.
reqs = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
with open("requirements.txt", "w") as f:
    f.write("\n".join(reqs) + "\n")
print("requirements:", len(reqs))

# 3. class_methods via the same introspection agents-cli uses.
ops = u._get_registered_operations(agent=agent_runtime)
class_methods = [
    u._to_dict(m)
    for m in u._generate_class_methods_spec_or_raise(agent=agent_runtime, operations=ops)
]
print("class_methods:", len(class_methods))

# 4. Memory Bank context spec (managed topics).
ctx = ReasoningEngineContextSpec(
    memory_bank_config=MemoryBankConfig(
        customization_configs=[
            CustomizationConfig(
                memory_topics=[
                    MemoryTopic(managed_memory_topic=ManagedMemoryTopic(
                        managed_topic_enum=ManagedTopicEnum.USER_PERSONAL_INFO)),
                    MemoryTopic(managed_memory_topic=ManagedMemoryTopic(
                        managed_topic_enum=ManagedTopicEnum.USER_PREFERENCES)),
                    MemoryTopic(managed_memory_topic=ManagedMemoryTopic(
                        managed_topic_enum=ManagedTopicEnum.EXPLICIT_INSTRUCTIONS)),
                ]
            )
        ]
    )
)

config = AgentEngineConfig(
    display_name="autoagents-agent",
    staging_bucket="gs://autoagents-500500-attachments",
    source_packages=["app"],
    entrypoint_module=ENTRY_MODULE,
    entrypoint_object=ENTRY_OBJECT,
    class_methods=class_methods,
    env_vars=env_vars,
    requirements_file="requirements.txt",
    context_spec=ctx,
    min_instances=1,
    max_instances=10,
    resource_limits={"cpu": "1", "memory": "4Gi"},
    container_concurrency=8,
    agent_framework="google-adk",
)

print("Updating engine via deployment_source WITH context_spec (3-5 min)...")
result = client.agent_engines.update(name=RES, config=config)
cs = getattr(result.api_resource, "context_spec", None)
print("DONE. context_spec present:", cs is not None)
