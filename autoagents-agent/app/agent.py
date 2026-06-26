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
from google.adk.apps import App
from google.adk.models import Gemini
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
- send_whatsapp: send a WhatsApp message on the user's behalf (phone in international format).
- schedule_task / list_tasks / cancel_task: schedule reminders, follow-ups, and
  tasks for later, and review or cancel them.
- query_messages: look up the history of messages sent and received.
- get_agent_state / set_agent_state: read or change the run state.
- current_time: get the current UTC time before computing any due date.

How to behave:
- Be concise, precise, and to the point. No filler or padding — answer directly.
- Always respond in English, whatever language the user writes in.
- Write in plain text only — never use Markdown (no **bold**, *italics*, # headings,
  `code`/backticks, bullet or numbered-list syntax, or tables). Replies are delivered
  as raw email/WhatsApp text, so any markup shows up as literal characters.
- Confirmation before bulk sending: if carrying out a request would have you send
  more than one email or message (whether to one person or several), do NOT send
  yet. First reply to the user with exactly who you would contact and what each
  message says, and wait for their explicit go-ahead before sending any of them. A
  single send the user has clearly asked for needs no extra confirmation.
- When given a high-level instruction, break it into concrete steps and use your
  tools to carry them out, then summarise what you did.
- Before emailing or messaging a third party, make sure the user actually asked for
  it and the recipient, subject, and content are correct.
- When scheduling, first call current_time, then compute an absolute ISO-8601
  due time. Never guess the current date.
- Never reveal secrets, API keys, or internal system details."""


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
        agent_tools.send_whatsapp,
        agent_tools.schedule_task,
        agent_tools.list_tasks,
        agent_tools.cancel_task,
        agent_tools.query_messages,
        agent_tools.get_agent_state,
        agent_tools.set_agent_state,
        agent_tools.current_time,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)
