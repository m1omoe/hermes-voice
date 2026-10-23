# Harness Voice backend — Feiniu (fnOS) Docker image
# Harness-agnostic base: FastAPI backend only. The agent CLI it drives
# (Claude Code / Codex / OpenCode / Hermes) is layered in later, once chosen.
FROM python:3.12-slim

# Local speech stack (faster-whisper STT + piper TTS, no API keys).
# Default ON for the Feiniu deployment.
ARG WITH_LOCAL_SPEECH=1

WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
COPY backend/app ./app

# av pinned: faster-whisper 1.2.x calls av.open(metadata_errors=...),
# removed in PyAV >= 15 — unpinned installs break audio decoding.
RUN pip install --no-cache-dir . \
    && if [ "$WITH_LOCAL_SPEECH" = "1" ]; then \
         pip install --no-cache-dir ".[local]" "av==14.2.0" \
         && mkdir -p /opt/piper-voices \
         && curl -sL -o /opt/piper-voices/zh_CN-huayan-medium.onnx \
              "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx" \
         && curl -sL -o /opt/piper-voices/zh_CN-huayan-medium.onnx.json \
              "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json"; \
       fi

# Persistent state (schedules/devices SQLite) lives in ~/.hermes-voice;
# whisper models cache in ~/.cache/huggingface — both volume-mounted.
VOLUME ["/root/.hermes-voice", "/root/.cache/huggingface"]
EXPOSE 8765
ENV HERMES_VOICE_HOST=0.0.0.0 HERMES_VOICE_PORT=8765 HERMES_VOICE_DISABLE_DOTENV=1
CMD ["python", "-m", "app"]
