"""Mouse-friendly terminal front end for the existing Claim-Agent CLI."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, Select, Static, TabbedContent, TabPane, TextArea

from .config import load_config
from .live_events import EventReader
from .live_log import LiveLogFormatter
from .tui_support import Attachment, child_options, feedback_command, native_input, parse_paths, prepare_request, read_state, run_command, validate_attachment

STAGES = {
    "ARCHITECT": "독립항 설계", "DRAFT": "의미 초안", "STYLE": "용어·문체 조정",
    "SUCCESS": "성공조건 검수", "SYNTAX": "통사·범위 검수", "OA": "OA 검수",
    "BLIND": "블라인드 복원", "PICTURE": "도면·기준 비교", "LOCK": "독립항 잠금",
    "DEP_ARCHITECT": "종속항 설계", "DEP_DRAFT": "종속항 초안", "DEP_STYLE": "종속항 문체",
    "DEP_SUCCESS": "종속항 성공조건", "DEP_SYNTAX": "종속항 통사", "DEP_OA": "종속항 OA",
    "DEP_RECON": "종속항 역구성", "DEP_LOCK": "종속항 잠금", "DONE": "완료", "HALTED": "검토 필요 / 중지",
}


class Composer(TextArea):
    async def _on_paste(self, event: events.Paste) -> None:
        # Terminal file drops arrive as pasted paths. Ordinary prose stays text.
        app = self.app
        if isinstance(app, ClaimAgentApp) and not app.running:
            try:
                paths = parse_paths(event.text, app.root)
            except (ValueError, OSError):
                paths = []
            if paths and all(p.is_file() for p in paths):
                event.stop()
                app.add_files(paths)
                return
        await super()._on_paste(event)


class ClaimAgentApp(App):
    TITLE = "Claim-Agent"
    SUB_TITLE = "Gemini · 청구항 작업실"
    BINDINGS = [
        Binding("ctrl+enter", "start", "실행", priority=True),
        Binding("f2", "pick", "파일 선택"),
        Binding("f3", "paste", "붙여넣기"),
        Binding("f6", "stop", "중지"),
        Binding("ctrl+q", "close_app", "종료", priority=True),
    ]
    CSS = """
    Screen { background: #101722; color: #e4eaf2; }
    Header { background: #1b2d42; }
    Footer { background: #1b2d42; }
    #status { height: 2; padding: 0 2; color: #8ad9ce; }
    #workspace { height: 1fr; }
    #compose-pane { width: 58%; padding: 0 1; border: round #3a526c; }
    #output-pane { width: 42%; padding: 0 1; border: round #3a526c; }
    .heading { height: 1; color: #8ad9ce; text-style: bold; margin-top: 1; }
    .hint { height: auto; color: #a9b7c8; margin-bottom: 1; }
    #editor-tabs { height: 10; }
    Composer { height: 6; border: solid #3a526c; }
    .row { height: 3; }
    .row Button { min-width: 10; width: auto; margin-right: 1; }
    #file-path { width: 1fr; }
    #attachments { height: 7; border: solid #3a526c; }
    #mode { width: 1fr; }
    #model { width: 1fr; }
    #dependent { width: 24; }
    #dep-target { width: 1fr; }
    #actions { height: 3; padding: 0 1; }
    #actions Button { min-width: 12; margin-right: 1; }
    #toggle-logs { height: 3; width: 1fr; }
    #feedback { height: 3; width: 14; }
    #log-panel { height: 45%; min-height: 6; }
    #logs { height: 1fr; }
    #result { height: 1fr; border: none; }
    #run-label { height: auto; color: #a9b7c8; }
    """

    def __init__(self, project_root: Path, files: list[str] | None = None, config_path: Path | None = None):
        super().__init__()
        self.root = project_root.resolve()
        self.config_path = config_path
        self.cfg = load_config(config_path, self.root)
        self.attachments: list[Attachment] = []
        self.initial_files = files or []
        self.running = False
        self.stop_requested = False
        self.process: asyncio.subprocess.Process | None = None
        self.run_id: str | None = None
        self.started = 0.0
        self.seen_records: set[str] = set()
        self.last_stage = ""
        self.result_text = ""
        self.log_text = ""
        self.log_open = False
        self.chat_history: list[dict] = []
        self.active_mode = "CHAT"
        self.job_dir: Path | None = None
        self.event_reader: EventReader | None = None
        self.chat_prefix = ""
        self.chat_stream = ""
        self.log_formatter = LiveLogFormatter()
        self.last_pipeline_run: str | None = None
        self.previous_report_stamp = 0

    def compose(self) -> ComposeResult:
        yield Header()
        key = os.environ.get(self.cfg.api_key_env) or (self.cfg.provider.kind == "gemini" and os.environ.get("GOOGLE_API_KEY"))
        auth = "구독 OAuth · 웹 모델 설정에서 연결" if self.cfg.provider.kind.endswith("_oauth") else f"API 키 {'저장됨' if key else '없음 — .env에 입력 필요'}"
        yield Static(f"{self.cfg.default_model}  ·  {auth}  ·  준비", id="status", markup=False)
        with Horizontal(id="workspace"):
            with VerticalScroll(id="compose-pane"):
                yield Label("01  프롬프트", classes="heading")
                yield Select([("자동 분류 · 대화", "CHAT"), ("청구항 잠정안 작성", "AUTHORING_DRAFT"), ("기존 작업에 피드백 반영", "FEEDBACK"), ("출원용 최종 검증", "FINALIZATION")], value="CHAT", allow_blank=False, id="mode")
                yield Select([], prompt="피드백을 반영할 작업 선택", id="feedback-run")
                yield Static("질문이나 수정할 내용을 아래에 입력하세요. 답변을 받은 뒤 이어서 피드백을 보낼 수 있습니다.", classes="hint")
                with TabbedContent(id="editor-tabs"):
                    with TabPane("대화·요청·피드백", id="request-tab"):
                        yield Composer(id="request")
                    with TabPane("발명 설명 붙여넣기", id="material-tab"):
                        yield Composer(id="material")
                yield Label("02  첨부 자료", classes="heading")
                with Horizontal(classes="row"):
                    yield Button("파일 선택", id="pick")
                    yield Button("붙여넣기", id="paste")
                with Horizontal(classes="row"):
                    yield Input(placeholder='파일 경로를 끌어 넣거나 붙여넣기 → 추가', id="file-path")
                    yield Button("추가", id="add")
                yield DataTable(id="attachments", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="row"):
                    yield Button("선택 삭제", id="remove")
                    yield Button("전체 비우기", id="clear")
                yield Static("TXT·MD·PNG·JPG·WEBP 등 지원. 모든 자료를 여기에 함께 넣으세요.", classes="hint")
                yield Label("03  작성 설정", classes="heading")
                yield Input(value=self.cfg.default_model, placeholder="모델 ID", id="model")
                with Horizontal(classes="row"):
                    yield Checkbox("종속항도 작성", id="dependent")
                    yield Input(value="2~8", placeholder="종속항 범위", id="dep-target")
                yield Static("실행 버튼을 누르면 첨부한 자료를 Gemini API로 전송합니다.", classes="hint")
            with Vertical(id="output-pane"):
                yield Label("진행 상황과 결과", classes="heading")
                yield Static("실행 대기", id="run-label", markup=False)
                yield TextArea("단순 대화에서는 자료 없이 질문할 수 있습니다. 청구항 작성은 왼쪽 작업 모드에서 선택하세요.", read_only=True, id="result")
                with Horizontal(classes="row"):
                    yield Button("결과 수정", id="feedback", disabled=True)
                    yield Button("실시간 로그 펼치기", id="toggle-logs")
                with Vertical(id="log-panel"):
                    yield TextArea(read_only=True, id="logs")
        with Horizontal(id="actions"):
            yield Button("실행", variant="success", id="start")
            yield Button("중지", variant="error", id="stop", disabled=True)
            yield Button("결과 복사", id="copy", disabled=True)
            yield Button("결과 폴더", id="folder")
            yield Button("새 대화", id="new-chat")
            yield Button("종료", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#attachments", DataTable).add_column("첨부 파일")
        if self.initial_files:
            self.add_files([Path(p).resolve() for p in self.initial_files])
        self.query_one("#request", TextArea).focus()
        self.set_log_open(False)
        self.refresh_runs()
        self.update_mode()
        self.add_log("파일 선택: F2  |  클립보드의 파일·이미지·텍스트: F3")
        self.add_log("드래그가 동작하지 않는 터미널에서는 탐색기에서 복사한 뒤 붙여넣기 버튼을 누르세요.")
        self.set_interval(0.2, self.poll_state)

    def redact(self, text: str) -> str:
        for name in {self.cfg.model.api_key_env, self.cfg.provider.anthropic.api_key_env, "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"}:
            value = os.environ.get(name)
            if value:
                text = text.replace(value, "[API KEY]")
        return text

    def add_log(self, text: str) -> None:
        self.log_text += self.redact(text) + "\n"
        self.refresh_log()

    def refresh_log(self) -> None:
        editor = self.query_one("#logs", TextArea)
        # Full call inputs and outputs stay in the run folder; bound the terminal render size but keep the start.
        text = self.log_text if len(self.log_text) <= 300000 else self.log_text[:60000] + "\n\n…(실시간 로그 일부 생략)…\n\n" + self.log_text[-240000:]
        editor.load_text(text)
        editor.scroll_end(animate=False)

    def set_log_open(self, opened: bool) -> None:
        self.log_open = opened
        self.query_one("#log-panel").display = opened
        self.query_one("#toggle-logs", Button).label = "실시간 로그 접기" if opened else "실시간 로그 펼치기"

    def update_mode(self) -> None:
        mode = self.query_one("#mode", Select).value
        self.query_one("#dependent", Checkbox).disabled = mode in {"CHAT", "FEEDBACK"}
        self.query_one("#dep-target", Input).disabled = mode in {"CHAT", "FEEDBACK"}
        self.query_one("#feedback-run", Select).display = mode == "FEEDBACK"
        self.query_one("#start", Button).label = "보내기" if mode == "CHAT" else ("수정 실행" if mode == "FEEDBACK" else "실행")

    def refresh_runs(self) -> None:
        root = self.cfg.path("runs_dir")
        options = []
        if root.exists():
            for path in sorted(root.glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
                state = read_state(path)
                if state and state.get("request_mode") in {"AUTHORING_DRAFT", "FINALIZATION"}:
                    options.append((f"{path.parent.name} · {state.get('outcome', '')}", path.parent.name))
        selector = self.query_one("#feedback-run", Select)
        selected = selector.value
        selector.set_options(options)
        available = {value for _, value in options}
        if self.last_pipeline_run in available:
            selector.value = self.last_pipeline_run
        elif selected in available:
            selector.value = selected

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "mode":
            self.update_mode()

    @staticmethod
    def chat_transcript(history: list[dict]) -> str:
        return "\n\n".join(("나" if m["role"] == "user" else "Gemini") + ":\n" + m["parts"][0].get("text", "") for m in history)

    def add_files(self, paths: list[Path]) -> None:
        if self.running:
            return
        added = 0
        for path in paths:
            try:
                item = validate_attachment(path)
                if any(a.path == item.path for a in self.attachments):
                    continue
                self.attachments.append(item)
                added += 1
            except ValueError as exc:
                self.add_log(str(exc))
                self.notify(str(exc), severity="warning")
        self.refresh_attachments()
        if added:
            self.add_log(f"자료 {added}개 추가됨 · 전체 {len(self.attachments)}개")

    def refresh_attachments(self) -> None:
        table = self.query_one("#attachments", DataTable)
        table.clear()
        for i, item in enumerate(self.attachments):
            table.add_row(item.path.name, key=str(i))

    def action_add(self) -> None:
        try:
            field = self.query_one("#file-path", Input)
            self.add_files(parse_paths(field.value, self.root))
            field.value = ""
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="warning")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "file-path":
            self.action_add()

    def action_pick(self) -> None:
        if not self.running:
            self.get_native("pick", self.focused)

    def action_paste(self) -> None:
        if not self.running:
            self.get_native("paste", self.focused)

    @work(exclusive=True, group="native")
    async def get_native(self, action: str, target) -> None:
        try:
            data = await native_input(self.root, action)
            if self.running:
                return
            if data.get("error"):
                self.notify(data["error"], severity="warning")
            elif data.get("paths"):
                self.add_files([Path(p).resolve() for p in data["paths"]])
            elif data.get("text"):
                text = data["text"]
                try:
                    paths = parse_paths(text, self.root)
                except (ValueError, OSError):
                    paths = []
                if paths and all(p.is_file() for p in paths):
                    self.add_files(paths)
                elif isinstance(target, Composer):
                    target.insert(text)
                    target.focus()
                elif isinstance(target, Input) and target.id == "file-path":
                    target.value = text
                    self.action_add()
                else:
                    self.query_one("#editor-tabs", TabbedContent).active = "material-tab"
                    editor = self.query_one("#material", Composer)
                    editor.insert(text)
                    editor.focus()
                    self.notify("클립보드 텍스트를 발명 설명에 넣었습니다.")
        except (ValueError, OSError, TimeoutError) as exc:
            self.notify(self.redact(str(exc)), severity="error")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "start":
            self.action_start()
        elif action == "stop":
            await self.action_stop()
        elif action == "pick":
            self.action_pick()
        elif action == "paste":
            self.action_paste()
        elif action == "add":
            self.action_add()
        elif action == "remove" and not self.running:
            table = self.query_one("#attachments", DataTable)
            if self.attachments:
                self.attachments.pop(table.cursor_row)
                self.refresh_attachments()
        elif action == "clear" and not self.running:
            self.attachments.clear()
            self.refresh_attachments()
        elif action == "folder":
            self.open_folder()
        elif action == "copy":
            self.copy_result()
        elif action == "toggle-logs":
            self.set_log_open(not self.log_open)
        elif action == "feedback" and not self.running:
            self.query_one("#mode", Select).value = "CHAT" if self.active_mode == "CHAT" else "FEEDBACK"
            self.refresh_runs()
            self.query_one("#editor-tabs", TabbedContent).active = "request-tab"
            self.query_one("#request", Composer).clear()
            self.query_one("#request", Composer).focus()
            self.notify("프롬프트에 수정할 내용을 입력한 뒤 보내세요.")
        elif action == "new-chat" and not self.running:
            self.chat_history = []
            self.last_pipeline_run = None
            self.active_mode = "CHAT"
            self.query_one("#mode", Select).value = "CHAT"
            self.run_id = None
            self.job_dir = None
            self.event_reader = None
            self.query_one("#run-label", Static).update("새 대화")
            self.result_text = ""
            self.query_one("#result", TextArea).load_text("새 대화를 시작합니다.")
            self.query_one("#request", Composer).clear()
            self.query_one("#material", Composer).clear()
            self.attachments.clear()
            self.refresh_attachments()
            self.log_text = ""
            self.refresh_log()
            self.set_log_open(False)
            self.query_one("#copy", Button).disabled = True
            self.query_one("#feedback", Button).disabled = True
        elif action == "quit":
            await self.action_close_app()

    def action_start(self) -> None:
        if self.running:
            return
        model = self.query_one("#model", Input).value.strip()
        if not model:
            self.notify("모델 ID를 입력하세요.", severity="warning")
            return
        from .model_settings import connection

        if not self.cfg.provider.kind.endswith("_oauth") and not connection(self.cfg, self.cfg.provider.kind)["connected"]:
            self.notify(".env에 API 키를 저장한 뒤 프로그램을 다시 열어 주세요.", severity="error")
            return
        run_id = time.strftime("tui-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        self.active_mode = str(self.query_one("#mode", Select).value)
        self.routed_mode = None
        self.routed_run_id = None
        request_text = self.query_one("#request", Composer).text.strip()
        try:
            if self.active_mode == "CHAT":
                if not request_text:
                    raise ValueError("메시지를 입력해 주세요.")
                self.job_dir = self.root / ".tui" / "requests" / run_id
                self.job_dir.mkdir(parents=True, exist_ok=False)
                request_path = self.job_dir / "chat.json"
                request_path.write_text(json.dumps({"text": request_text, "material": self.query_one("#material", Composer).text,
                    "files": [str(a.path) for a in self.attachments],
                    "attachments": [{"path": str(a.path), "category": a.category} for a in self.attachments],
                    "history": self.chat_history, "model": model, "run_id": self.last_pipeline_run}, ensure_ascii=False), encoding="utf-8")
                command = [sys.executable, "-u", "-m", "claim_agent.chat", "--project-root", str(self.root), "--request", str(request_path)]
                if self.config_path:
                    command += ["--config", str(self.config_path)]
            elif self.active_mode == "FEEDBACK":
                selected = self.query_one("#feedback-run", Select).value
                if selected is Select.BLANK:
                    raise ValueError("피드백을 반영할 기존 작업을 선택해 주세요.")
                state = read_state(self.cfg.path("runs_dir") / str(selected) / "state.json")
                if not state:
                    raise ValueError("기존 작업 기록을 읽을 수 없습니다.")
                self.job_dir = self.root / ".tui" / "requests" / run_id
                command = feedback_command(self.root, self.job_dir, state, request_text,
                    self.query_one("#material", Composer).text, self.attachments, model)
                run_id = str(selected)
                if self.config_path:
                    command[command.index("resume"):command.index("resume")] = ["--config", str(self.config_path)]
            else:
                request_path = prepare_request(
                    self.root, run_id, request_text,
                    self.query_one("#material", Composer).text, self.attachments,
                    self.query_one("#dependent", Checkbox).value,
                    self.query_one("#dep-target", Input).value, self.active_mode,
                )
                self.job_dir = request_path.parent
                command = run_command(self.root, request_path, run_id, model)
                if self.config_path:
                    command[command.index("run"):command.index("run")] = ["--config", str(self.config_path)]
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.run_id = run_id
        previous_report = self.cfg.path("runs_dir") / run_id / "report.md"
        self.previous_report_stamp = previous_report.stat().st_mtime_ns if previous_report.exists() else 0
        self.stop_requested = False
        self.seen_records.clear()
        self.last_stage = ""
        self.started = time.monotonic()
        self.result_text = ""
        self.chat_stream = ""
        self.chat_prefix = (self.chat_transcript(self.chat_history) + "\n\n나:\n" + request_text + "\n\nGemini:\n").lstrip()
        self.query_one("#result", TextArea).load_text(self.chat_prefix if self.active_mode == "CHAT" else "작업 진행 중입니다.")
        self.event_reader = EventReader(self.job_dir / "events.jsonl")
        self.query_one("#copy", Button).disabled = True
        self.set_log_open(True)
        self.query_one("#run-label", Static).update(run_id)
        self.set_running(True)
        self.add_log(f"\n시작 · {model} · {run_id}")
        self.execute(command)

    def set_running(self, running: bool) -> None:
        self.running = running
        self.query_one("#compose-pane").disabled = running
        self.query_one("#start", Button).disabled = running
        self.query_one("#stop", Button).disabled = not running
        self.query_one("#new-chat", Button).disabled = running
        self.query_one("#feedback", Button).disabled = running or not self.result_text

    @work(exclusive=True, group="pipeline")
    async def execute(self, command: list[str]) -> None:
        try:
            opts = child_options(self.root)
            opts.pop("encoding")
            opts.pop("errors")
            if self.event_reader:
                opts["env"]["CLAIM_AGENT_EVENT_LOG"] = str(self.event_reader.path)
            self.process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **opts,
            )
            if self.stop_requested:
                self.process.terminate()
            assert self.process.stdout is not None
            async for line in self.process.stdout:
                self.add_log(line.decode("utf-8", errors="replace").rstrip())
            code = await self.process.wait()
            self.poll_state()
            if self.stop_requested:
                message = "사용자 중지 · 저장된 기록은 결과 폴더에 보존됩니다."
            else:
                state = self.current_state()
                outcome = state.get("outcome", "") if state else ""
                message = f"종료 · {outcome or '완료'}" if code == 0 else f"종료 · 오류 또는 검토 필요 (코드 {code}) · {outcome}"
            self.add_log(message)
            self.query_one("#status", Static).update(message)
            self.show_report()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.add_log(f"실행 오류: {exc}")
            self.query_one("#status", Static).update("실행 오류 · 진행 로그를 확인하세요.")
        finally:
            if self.process and self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
            self.process = None
            self.set_running(False)
            self.set_log_open(False)
            self.refresh_runs()

    def current_state(self) -> dict | None:
        if self.active_mode == "CHAT" and getattr(self, "routed_mode", None) not in (None, "CHAT", "META"):
            return read_state(self.cfg.path("runs_dir") / self.routed_run_id / "state.json") if self.routed_run_id else None
        return read_state(self.cfg.path("runs_dir") / self.run_id / "state.json") if self.run_id and self.active_mode != "CHAT" else None

    def poll_events(self) -> None:
        if not self.event_reader:
            return
        try:
            rows = self.event_reader.read()
        except (OSError, ValueError):
            return
        formatter = self.log_formatter
        for event in rows:
            kind = event["kind"]
            if kind == "route":
                self.routed_mode = event["mode"]
                self.routed_run_id = event.get("run_id")
                self.log_text += f"\n[자동 분류: {event['mode']}] {event.get('reason', '')}\n"
                continue
            if kind == "delta" and self.active_mode == "CHAT" and event.get("role") == "대화" and not event.get("target"):
                self.chat_stream += event.get("text", "")
            self.log_text += formatter.feed(event)
        if rows:
            self.log_text = self.redact(self.log_text)
            self.refresh_log()
            if self.active_mode == "CHAT" and getattr(self, "routed_mode", None) in (None, "CHAT", "META"):
                self.query_one("#result", TextArea).load_text(self.redact(self.chat_prefix + self.chat_stream))
            elif self.active_mode == "CHAT":
                self.query_one("#result", TextArea).load_text(f"자동 분류: {self.routed_mode} — 전문 에이전트 검증 진행 중입니다. 작업 로그에서 단계별 결과를 확인할 수 있습니다.")

    def poll_state(self) -> None:
        if not self.running:
            return
        self.poll_events()
        state = self.current_state()
        stage = state.get("stage", "") if state else ""
        label = (STAGES.get(stage, stage) or getattr(self, "routed_mode", None) or "요청 분류 / 응답 대기")
        seconds = int(time.monotonic() - self.started)
        self.query_one("#status", Static).update(f"실행 중 · 최근 저장 단계: {label} · {seconds // 60:02d}:{seconds % 60:02d}")
        if not state:
            return
        if stage != self.last_stage:
            self.add_log(f"단계 · {label}")
            self.last_stage = stage
        for rid, record in state.get("records", {}).items():
            if rid not in self.seen_records:
                self.add_log(f"{record.get('role', '')} → {record.get('status', '')}")
                self.seen_records.add(rid)

    def show_report(self) -> None:
        if not self.run_id:
            return
        if self.active_mode == "CHAT":
            response = read_state(self.job_dir / "response.json") if self.job_dir else None
            if response and not self.stop_requested:
                self.chat_history = response["history"]
                self.result_text = self.redact(response["answer"] if response.get("run_id") else self.chat_transcript(self.chat_history))
                if response.get("run_id") and response.get("effective_mode") in {"AUTHORING_DRAFT", "FINALIZATION"}:
                    self.last_pipeline_run = response["run_id"]
                self.query_one("#result", TextArea).load_text(self.result_text)
                self.query_one("#request", Composer).clear()
                self.query_one("#copy", Button).disabled = False
                if response.get("finish_reason") == "MAX_TOKENS":
                    self.notify("응답 길이 제한에 도달했습니다. 이어서 설명해 달라고 요청할 수 있습니다.")
            else:
                self.query_one("#result", TextArea).load_text(self.redact(self.chat_prefix + self.chat_stream + "\n\n[응답 미완료 — 로그 펼치기로 확인하세요. 이전 대화는 유지됩니다.]"))
            return
        report = self.cfg.path("runs_dir") / self.run_id / "report.md"
        if self.active_mode == "FEEDBACK" and report.exists() and report.stat().st_mtime_ns == self.previous_report_stamp:
            self.query_one("#result", TextArea).load_text("수정된 보고서가 생성되지 않았습니다. 로그 펼치기로 중지 원인을 확인하세요. 이전 보고서는 결과 폴더에 보존되어 있습니다.")
            return
        concise = self.cfg.path("runs_dir") / self.run_id / "report-chat.md"
        if report.exists():
            self.last_pipeline_run = self.run_id
            self.result_text = self.redact((concise if concise.exists() else report).read_text(encoding="utf-8"))
            self.query_one("#result", TextArea).load_text(self.result_text)
            self.query_one("#copy", Button).disabled = False
        else:
            self.query_one("#result", TextArea).load_text("완료된 보고서가 없습니다. 진행 로그와 결과 폴더의 저장 기록을 확인하세요.")

    async def action_stop(self) -> None:
        if not self.running:
            return
        self.stop_requested = True
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.add_log("중지 요청 · 이미 전송된 API 요청에는 사용량이 발생할 수 있습니다.")

    async def action_close_app(self) -> None:
        await self.action_stop()
        self.exit()

    def open_folder(self) -> None:
        folder = self.cfg.path("runs_dir")
        if self.active_mode == "CHAT" and self.job_dir:
            folder = self.job_dir
        elif self.run_id and (folder / self.run_id).exists():
            folder /= self.run_id
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(folder)
            else:
                self.notify(str(folder))
        except OSError as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True, group="clipboard-copy")
    async def copy_result(self) -> None:
        if self.result_text:
            try:
                if os.name == "nt":
                    result = await asyncio.to_thread(
                        subprocess.run, ["powershell.exe", "-NoProfile", "-STA", "-Command",
                                         "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::SetText([Console]::In.ReadToEnd())"],
                        input=self.result_text, capture_output=True, timeout=15, **child_options(self.root),
                    )
                    if result.returncode:
                        raise OSError("클립보드 복사에 실패했습니다.")
                else:
                    self.copy_to_clipboard(self.result_text)
                self.notify("결과 보고서를 복사했습니다.")
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.notify(str(exc), severity="error")


ClaimCopaApp = ClaimAgentApp  # Compatibility for existing Python callers.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claim-Agent 터미널 작업실")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    ClaimAgentApp(args.project_root, args.files, args.config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
