"""视频探测、字幕预览、产物读取和字幕编辑。"""
from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..core.errors import AppError
from ..core.events import AppEvent
from ..services.ass_document import parse_ass
from ..services.subtitler import validate_srt
from .runtime import EngineRuntime
from .schemas import ProbeIn, SubtitleIn

log = logging.getLogger(__name__)


def media_routes(runtime: EngineRuntime) -> APIRouter:
    router = APIRouter()

    def _artifact_or_404(aid: int) -> dict:
        a = runtime.store.get_artifact(aid)
        if not a:
            raise HTTPException(404, "产物不存在")
        return a

    @router.post("/api/v1/probe")
    def probe(body: ProbeIn):
        """两阶段交互第一步：探测可用分辨率（缓存 24h）。"""
        try:
            engine, result = runtime.router.probe(body.url, use_cache=not body.force_refresh)
        except AppError as e:
            raise HTTPException(422, e.to_dict())
        return {"engine": engine.name, "title": result.title,
                "duration": result.duration,
                "thumbnail": getattr(result, "thumbnail", ""),
                "quality_notice": result.quality_notice,
                "auth_status": result.auth_status,
                "formats": [asdict(f) for f in result.formats]}

    @router.get("/api/v1/preview/subtitle")
    def subtitle_preview(text: str = "字幕预览 Subtitle 123",
                          font_name: str = "", font_size: int = 24,
                          position: str = "bottom", outline: int = 1,
                          color: str = "#FFFFFF", vertical_offset: int = 0,
                          horizontal_offset: int = 0, person_notes: bool = False):
        """字幕样式预览帧：按所选字体/字号/位置渲染样例文本（PNG）。

        结果按参数哈希缓存，同参数秒回。
        """
        import hashlib
        from app.services.renderer import render_preview_png
        pos = position if position in ("bottom", "middle", "top") else "bottom"
        if (not re.fullmatch(r"#[0-9a-fA-F]{6}", color)
                or not -120 <= vertical_offset <= 120
                or not -270 <= horizontal_offset <= 270):
            raise HTTPException(422, "字幕颜色或位置偏移无效")
        try:
            width = max(0, min(8, int(outline)))
        except (TypeError, ValueError):
            width = 1
        # 定位样式已更新，旧缓存中的字幕大小和锚点不再适用。
        key = hashlib.sha1(
            f"2|{text}|{font_name}|{font_size}|{pos}|{width}|{color}|{vertical_offset}|{horizontal_offset}|{person_notes}".encode(),
            usedforsecurity=False).hexdigest()
        cache_dir = runtime.data_root / "cache" / "preview"
        png = cache_dir / f"{key}.png"
        if not png.exists():
            try:
                render_preview_png(runtime.config, text, font_name, font_size, pos, png,
                                   outline=width, color=color,
                                   vertical_offset=vertical_offset,
                                   horizontal_offset=horizontal_offset,
                                   **({"person_notes": True} if person_notes else {}))
            except AppError as e:
                raise HTTPException(500, e.to_dict())
        return FileResponse(png, media_type="image/png")

    @router.get("/api/v1/artifacts/{aid}/content")
    def artifact_content(aid: int):
        """读取字幕文本（编辑器用）。"""
        a = _artifact_or_404(aid)
        if a["kind"] not in ("subtitle", "bilingual", "person_notes"):
            raise HTTPException(400, "仅字幕产物支持文本读写")
        p = Path(a["path"])
        if not p.exists():
            raise HTTPException(404, "字幕文件不存在")
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        if a["kind"] == "person_notes":
            return {"text": text, "source_text": "", "source_language": "", "is_source": False}
        source_language, source_text = '', ''
        task = runtime.store.get_task(a['task_id']) or {}
        if re.fullmatch(r'[A-Za-z0-9_-]+', a['task_id']):
            cached = runtime.data_root / 'cache' / 'transcripts' / (a['task_id'] + '.json')
            try:
                source_language = str(json.loads(cached.read_text(encoding='utf-8')).get('language') or '')
            except (OSError, ValueError, AttributeError, UnicodeError):
                source_language = str(task.get('source_lang') or '')
        is_source = bool(source_language and a.get('lang') == source_language and a['kind'] == 'subtitle')
        if source_language and not is_source:
            for original in reversed(runtime.store.list_artifacts(a['task_id'])):
                if original['kind'] == 'subtitle' and original.get('lang') == source_language:
                    try:
                        source_text = Path(original['path']).read_text(encoding='utf-8-sig')
                    except (OSError, UnicodeError):
                        continue
                    break
        return {'text': text, 'source_text': source_text, 'source_language': source_language, 'is_source': is_source}

    def _editable_subtitle(aid: int) -> tuple[dict, Path]:
        a = _artifact_or_404(aid)
        path = Path(a["path"])
        extensions = {"subtitle": ".srt", "bilingual": ".srt", "person_notes": ".ass"}
        if extensions.get(a["kind"]) != path.suffix.casefold():
            raise HTTPException(400, "仅支持编辑已登记的 SRT 与人名注释 ASS")
        if not path.is_file():
            raise HTTPException(404, "字幕文件不存在，请重新生成字幕")
        if runtime.store.artifact_owned_by_other(a["task_id"], str(path)):
            raise HTTPException(409, "此字幕被多个历史任务共用，无法直接覆盖；请先保留独立副本")
        return a, path

    @router.put("/api/v1/artifacts/{aid}/content")
    def save_artifact_content(aid: int, body: SubtitleIn):
        """文件路径来自已登记的字幕产物。"""
        a, path = _editable_subtitle(aid)
        try:
            (parse_ass if a["kind"] == "person_notes" else validate_srt)(body.text)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        task = runtime.store.get_task(a["task_id"])
        if not task:
            raise HTTPException(409, "任务正在处理，请完成后再修改字幕")
        with runtime.terminal_operation(task, "任务正在处理，请完成后再修改字幕") as task:
            # 等待领取期间字幕可能已被移走或删除，必须重读当前登记。
            a, path = _editable_subtitle(aid)
            if a["task_id"] != task["id"]:
                raise HTTPException(409, "字幕已不属于此任务，请刷新后重试")
            try:
                (parse_ass if a["kind"] == "person_notes" else validate_srt)(body.text)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="\n",
                                                 dir=path.parent, prefix=".subtitle-", suffix=".tmp",
                                                 delete=False) as f:
                    temporary = Path(f.name)
                    f.write(body.text)
                temporary.replace(path)
            except OSError as e:
                raise HTTPException(500, "保存字幕失败，请检查文件权限或占用情况") from e
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        log.warning("字幕临时文件清理失败：%s", temporary)
            runtime.events.publish(AppEvent(type="task", task_id=task["id"], status="updated"))
        return {"ok": True, "path": a["path"]}

    @router.get("/api/v1/artifacts/{aid}/file")
    def artifact_file(aid: int):
        """产物文件下载（按数据库 id 映射路径，不接受客户端路径）。"""
        a = _artifact_or_404(aid)
        p = Path(a["path"])
        if not p.exists():
            raise HTTPException(404, "文件不存在")
        return FileResponse(p, filename=p.name)

    @router.post("/api/v1/tasks/{tid}/rerender")
    def rerender(tid: str, body: dict | None = None):
        """字幕编辑后重渲染：复用视频与 SRT，仅重跑 render 阶段。"""
        body = body or {}
        lang = body.get("lang") or ""
        artifact_id = body.get("artifact_id")
        background = body.get("background", False)
        if not isinstance(lang, str) or (artifact_id is None and not lang):
            raise HTTPException(400, "请选择要用于重新生成视频的字幕")
        if artifact_id is not None and (type(artifact_id) is not int or artifact_id <= 0):
            raise HTTPException(400, "字幕产物编号无效")
        if type(background) is not bool:
            raise HTTPException(400, "background 参数必须为布尔值")
        try:
            outputs = runtime.runner.rerender_subtitles(tid, lang, artifact_id=artifact_id, background=background)
        except AppError as e:
            raise HTTPException(422, e.to_dict())
        return {"ok": True, "status": "running" if background else "completed",
                "outputs": [str(o) for o in outputs]}

    return router
