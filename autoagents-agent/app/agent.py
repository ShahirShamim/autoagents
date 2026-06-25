# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

import google
import vertexai
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.genai import types

from app import tools as agent_tools
from app.retrievers import create_search_tool

LLM_LOCATION = "global"
LOCATION = "us-central1"
LLM = "gemini-3.5-flash"

credentials, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = LLM_LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

vertexai.init(project=project_id, location=LOCATION)


data_store_region = os.getenv("DATA_STORE_REGION", "global")
data_store_id = os.getenv("DATA_STORE_ID", "autoagents-agent-collection_documents")
data_store_path = (
    f"projects/{project_id}/locations/{data_store_region}"
    f"/collections/default_collection/dataStores/{data_store_id}"
)

vertex_search_tool = create_search_tool(data_store_path)


instruction = """You are "autoagents", a personal autonomous assistant that talks
with the user over email. You can read attachments the user sends (text, images,
PDFs, audio, short video) and reason over them.

Tools you have:
- search_documents: search the user's long-term document store (RAG Engine).
- ingest_document: add a document (by gs:// URI) to the long-term store.
- you also automatically recall facts/preferences the user told you in past
  conversations (Memory Bank) — use them to personalise your replies.
- send_email: send an email on the user's behalf, or to reply/report to the user.
- schedule_task / list_tasks / cancel_task: schedule reminders, follow-ups, and
  tasks for later, and review or cancel them.
- query_messages: look up the history of messages sent and received.
- get_agent_state / set_agent_state: read or change the run state.
- current_time: get the current UTC time before computing any due date.

How to behave:
- Be concise and helpful. When given a high-level instruction, break it into
  concrete steps and use your tools to carry them out, then summarise what you did.
- Before emailing a third party, make sure the user actually asked for it and the
  recipient, subject, and content are correct.
- When scheduling, first call current_time, then compute an absolute ISO-8601
  due time. Never guess the current date.
- Never reveal secrets, API keys, or internal system details."""


async def generate_memories_callback(callback_context: CallbackContext) -> None:
    """After each turn, send the session to Memory Bank so the agent remembers
    the user's facts and preferences in future conversations."""
    await callback_context.add_session_to_memory()
    return None


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-3.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=instruction,
    tools=[
        agent_tools.search_documents,
        agent_tools.ingest_document,
        agent_tools.send_email,
        agent_tools.schedule_task,
        agent_tools.list_tasks,
        agent_tools.cancel_task,
        agent_tools.query_messages,
        agent_tools.get_agent_state,
        agent_tools.set_agent_state,
        agent_tools.current_time,
        # Memory Bank: auto-injects relevant past memories at the start of each turn.
        PreloadMemoryTool(),
    ],
    # Memory Bank: persists each conversation's takeaways for future recall.
    after_agent_callback=generate_memories_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)
