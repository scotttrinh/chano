---
title: Tutorial
description: Build a full-stack AI Data & Document Assistant with Chano, Next.js, sqlstruct, and Vercel services.
lede: Step-by-step tutorial building a single-user AI chat app and evolving it with file uploads, sandboxed execution, authentication, durable workflows, queues, and schedules.
section: tutorial
order: 0
nav_title: Overview
---

## What you will build

A full-stack **AI Data & Document Assistant** with a Next.js frontend and a Chano backend deployed as Vercel Services:

1. [Project setup](/tutorial/01-project-setup): Initialize the full-stack workspace with `vercel.json`, Next.js, and Chano.
2. [Streaming chat](/tutorial/02-streaming-chat): Stream responses from Vercel AI Gateway using `SSEResponse` and `msgspec.Struct`.
3. [File uploads](/tutorial/03-file-uploads): Upload CSV and document attachments to Vercel Blob.
4. [Sandbox execution](/tutorial/04-sandbox-execution): Run assistant-generated Python against uploaded files in Vercel Sandbox.
5. [Authentication](/tutorial/05-authentication): Add sign up, sign in, typed outcomes, route-group `boundary.py` guards, and per-user chat history with `sqlstruct`, `HTTPBearer`, `Depends()`, and `after()`.
6. [Durable workflows](/tutorial/06-durable-workflows): Trigger and poll long-running report jobs with `vercel-workflow` and Chano's native `[[tool.vercel.workflows]]` support.
7. [Queues & schedules](/tutorial/07-queues-and-schedules): Consume background events with `queue.py` and run periodic cleanup with `schedule.py`.
