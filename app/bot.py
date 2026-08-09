"""One Pipecat pipeline per WhatsApp call.

    caller audio ──▶ Sarvam Saaras (STT, codemix)
                        └─▶ Gemini Flash + TrekOps tools
                                └─▶ Sarvam Bulbul (TTS) ──▶ caller

The transport is SmallWebRTCTransport: WhatsApp Business Calling is WebRTC, so
Pipecat's WhatsApp client hands us a live peer connection and this pipeline just
plugs into it. No PSTN, no telephony vendor, no per-minute carrier cost.

IMPORT PATHS: Pipecat has reorganised its transport modules more than once. The
try/except shims below cover the layouts in circulation; pin `pipecat-ai` in
requirements.txt once you've confirmed which one your install uses.
"""

import time
from dataclasses import dataclass, field

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.transcript_processor import TranscriptProcessor
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService

try:  # pipecat >= 0.0.80 layout
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
except ImportError:  # older layout
    from pipecat.transports.network.small_webrtc import SmallWebRTCTransport  # type: ignore

try:
    from pipecat.transports.base_transport import TransportParams
except ImportError:  # pragma: no cover
    from pipecat.transports.base_transport import TransportParams  # type: ignore

from app import postcall, prompts
from app.config import settings
from app.tenancy import Company
from app.tools.definitions import TOOLS, build_handlers
from app.trekops import mongo


@dataclass
class CallSession:
    """Mutable per-call state. Tools write to it, postcall reads it."""

    call_id: str
    caller_phone: str
    company: Company
    started_at: float = field(default_factory=time.time)
    lead: dict = field(default_factory=dict)
    wants_whatsapp: bool = False
    turn_count: int = 0
    transcript: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)

    def log_tool(self, name: str, args: dict, result) -> None:
        # Truncated on purpose — a full departures payload per call bloats the
        # Call document for no diagnostic benefit.
        self.tool_calls.append({"name": name, "args": args, "result": str(result)[:500]})
        logger.info(f"[{self.call_id}] tool {name}({args})")

    def duration_seconds(self) -> int:
        return int(time.time() - self.started_at)

    def transcript_text(self) -> str:
        return "\n".join(f"{m['role']}: {m['content']}" for m in self.transcript)


async def run_bot(webrtc_connection, call_id: str, caller_phone: str, company: Company) -> None:
    """Build and run the pipeline for a single answered call."""

    session = CallSession(call_id=call_id, caller_phone=caller_phone, company=company)
    logger.info(f"[{call_id}] answering call from {caller_phone} for company={company.code}")

    # Who is calling — resolved before the greeting so the agent can open by name.
    caller_context = await mongo.get_caller_context(company, caller_phone)

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # Server-side VAD drives turn-taking and barge-in. Without it the
            # agent talks over the caller, which is the single most common
            # reason a voice agent feels broken.
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    stt = SarvamSTTService(
        api_key=settings.sarvam_api_key,
        model=settings.sarvam_stt_model,
        # "codemix" is what keeps "Kedarkantha ke liye 20 tarikh available hai?"
        # from being mangled by a monolingual model.
        mode=settings.sarvam_stt_mode,
    )

    tts = SarvamTTSService(
        api_key=settings.sarvam_api_key,
        settings=SarvamTTSService.Settings(
            voice=settings.sarvam_tts_voice,
            model=settings.sarvam_tts_model,
            language=settings.sarvam_tts_language,
        ),
    )

    llm = GoogleLLMService(api_key=settings.google_api_key, model=settings.gemini_model)

    for name, handler in build_handlers(session).items():
        llm.register_function(name, handler)

    context = OpenAILLMContext(
        messages=[
            {
                "role": "system",
                "content": prompts.build_system_prompt(company.name, caller_context),
            },
            {
                "role": "system",
                "content": (
                    "Open the call with exactly this, then wait: "
                    + prompts.AI_DISCLOSURE.format(company=company.name)
                ),
            },
        ],
        tools=TOOLS,
    )
    context_aggregator = llm.create_context_aggregator(context)

    transcript = TranscriptProcessor()

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            transcript.user(),
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            transcript.assistant(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,   # barge-in: caller can cut the agent off
            enable_metrics=True,        # feeds the per-call cost record
        ),
    )

    @transcript.event_handler("on_transcript_update")
    async def on_transcript_update(_processor, frame):
        for msg in frame.messages:
            session.transcript.append({"role": msg.role, "content": msg.content})
            if msg.role == "user":
                session.turn_count += 1

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        logger.info(f"[{call_id}] media connected — greeting")
        # Kick the LLM so the agent speaks first. A silent answer makes callers
        # say "hello? hello?" and burns the first ten seconds.
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        logger.info(f"[{call_id}] caller disconnected after {session.duration_seconds()}s")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    except Exception as err:
        logger.exception(f"[{call_id}] pipeline error: {err}")
    finally:
        # Runs whether the call ended cleanly, errored, or the caller hung up
        # mid-sentence — a lead captured at second 40 must not be lost because
        # second 41 threw.
        try:
            await postcall.run(session)
        except Exception as err:
            logger.exception(f"[{call_id}] post-call pipeline failed: {err}")
