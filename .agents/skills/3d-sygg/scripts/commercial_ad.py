#!/usr/bin/env python3
"""Stateful three-step orchestration for the 3D sygg Codex Skill."""

from __future__ import annotations

import argparse
import copy
from functools import wraps
from contextlib import ExitStack
import time
import json
import math
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

try:
    from .media import (
        extract_review_frames,
        image_pixel_hash,
        mix_narration,
        normalize_clip,
        probe_media,
        require_media_tools,
        stitch_clips,
        write_qa_report,
    )
    from .providers import (
        KEYCHAIN_ACCOUNT,
        CANGYUAN_KEYCHAIN_SERVICE,
        MINIMAX_KEYCHAIN_SERVICE,
        OMNI_KEYCHAIN_SERVICE,
        Keychain,
        file_lock,
        MiniMaxClient,
        CangyuanClient,
        OmniClient,
        PaidRequestBlocked,
        ProviderError,
        SubmissionUnknown,
        TaskLedger,
        TemporaryPublisher,
        VIDEO_MODEL_ID,
        ValidationError,
        atomic_write_json,
        canonical_hash,
        choose_system_voice,
        sha256_file,
        validate_reference_image,
    )
except ImportError:  # Direct script execution.
    from media import (
        extract_review_frames,
        image_pixel_hash,
        mix_narration,
        normalize_clip,
        probe_media,
        require_media_tools,
        stitch_clips,
        write_qa_report,
    )
    from providers import (
        KEYCHAIN_ACCOUNT,
        CANGYUAN_KEYCHAIN_SERVICE,
        MINIMAX_KEYCHAIN_SERVICE,
        OMNI_KEYCHAIN_SERVICE,
        Keychain,
        file_lock,
        MiniMaxClient,
        CangyuanClient,
        OmniClient,
        PaidRequestBlocked,
        ProviderError,
        SubmissionUnknown,
        TaskLedger,
        TemporaryPublisher,
        VIDEO_MODEL_ID,
        ValidationError,
        atomic_write_json,
        canonical_hash,
        choose_system_voice,
        sha256_file,
        validate_reference_image,
    )

try:
    from . import capabilities as caps
    from . import advanced
    from . import audio_routing
    from . import image_api
except ImportError:
    import capabilities as caps
    import advanced
    import audio_routing
    import image_api


STYLE_NAMES = {
    "chrome_puffy_3d": "3D 充气/镀铬潮酷风",
    "cyber_holographic_vfx": "未来赛博全息特效风",
    "cinematic_neo_noir": "新冷调商业电影风",
}
TALENT_MODE_NAMES = {
    "pure_product": "纯产品/免人物模式",
    "human_interaction": "人物/模特交互模式",
}
HUMAN_FRAMINGS = {"hands_only", "partial_body", "face_and_body"}
SILENT_CONSTRAINT = (
    "silent, no dialogue, no human voice, no speech, "
    "ambient SFX and mechanical sound effects only"
)
NORMAL_STATES = {
    "awaiting_plan_approval",
    "awaiting_reference_approval",
    "approved_for_generation",
    "generating",
    "delivered",
    "segment_ready",
}
ERROR_STATES = {"submission_unknown", "failed", "awaiting_manual_review"}
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
PROJECT_SCHEMA_VERSION = 10
SUPPORTED_ASPECT_RATIOS = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
}
STORYBOARD_LAYOUTS = {
    1: ("single_view", "单视图"),
    2: ("two_panel", "2 分格"),
    3: ("three_panel", "3 分格"),
    4: ("four_panel", "4 分格"),
}
CREATIVE_REVIEW_CHECKS = (
    "storyboard_sequence_followed",
    "product_identity_preserved",
    "approved_copy_only",
    "no_collage_grid_or_unwanted_text",
    "visual_quality_professional",
    "audio_review_passed",
    "motion_and_transitions_coherent",
    "talent_identity_and_anatomy_preserved",
)


def storyboard_reading_order(panel_count: int) -> str:
    """Return the visual reading order used by the approved storyboard sheet."""
    if panel_count not in STORYBOARD_LAYOUTS:
        raise ValidationError("Storyboard panel count must be between 1 and 4")
    if panel_count == 1:
        return "single full-frame composition"
    if panel_count in {2, 3}:
        return "left to right in one row"
    return "top-left, top-right, bottom-left, bottom-right"


def validate_target_contract(state: dict[str, Any]) -> None:
    """Keep the approved aspect ratio aligned across planning, provider, and post-production."""
    if state.get("schema_version", 0) >= 9:
        return advanced.validate_target(state)
    target = state.get("target")
    provider = state.get("provider_contract")
    if not isinstance(target, dict) or not isinstance(provider, dict):
        raise ValidationError("Project target or provider contract is missing")
    aspect_ratio = target.get("aspect_ratio")
    if not isinstance(aspect_ratio, str) or aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
        raise ValidationError("Project aspect ratio must be 9:16 or 16:9")
    expected_width, expected_height = SUPPORTED_ASPECT_RATIOS[str(aspect_ratio)]
    if (target.get("width"), target.get("height")) != (expected_width, expected_height):
        raise ValidationError("Project dimensions do not match the approved 720p aspect ratio")
    if target.get("resolution") != "720p":
        raise ValidationError("Project resolution must remain 720p")
    if provider.get("aspect_ratio") != aspect_ratio or provider.get("resolution") != "720p":
        raise ValidationError("Provider aspect ratio or resolution does not match the approved project target")


def balanced_keep_durations(target_duration: float) -> list[float]:
    """Distribute the requested final duration; 10s belongs only to the provider contract."""
    if (isinstance(target_duration, bool) or not isinstance(target_duration, (int, float))
            or not math.isfinite(target_duration) or target_duration <= 0):
        raise ValidationError("Target duration must be explicitly planned as positive finite seconds")
    clip_count = math.ceil(target_duration / 10)
    base = math.floor(target_duration / clip_count)
    remainder = target_duration - base * clip_count
    durations = [round(base + min(1, max(0, remainder - index)), 6) for index in range(clip_count)]
    if any(value <= 0 or value > 10 for value in durations):
        raise ValidationError("Could not distribute target duration into 10-second upstream requests")
    return durations


def materialize_shot_timeline(shots: list[dict[str, Any]], action_end: float) -> list[dict[str, Any]]:
    """Convert relative shot weights into deterministic time-coded beats."""
    if not 1 <= len(shots) <= 4:
        raise ValidationError("Each segment must use 1 to 4 storyboard beats under this workflow")
    weights = [float(row.get("duration_weight", 1.0)) for row in shots]
    if any(not math.isfinite(value) or value <= 0 for value in weights):
        raise ValidationError("Shot duration_weight values must be greater than zero")
    total = sum(weights)
    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    cumulative = 0.0
    for index, (row, weight) in enumerate(zip(shots, weights, strict=True), start=1):
        cumulative += weight
        end = action_end if index == len(shots) else round(action_end * cumulative / total, 2)
        timeline.append(
            {
                "index": index,
                "start": round(cursor, 2),
                "end": round(end, 2),
                "purpose": str(row["purpose"]).strip(),
                "visual": str(row["visual"]).strip(),
                "action": str(row["action"]).strip(),
                "product_detail": str(row["product_detail"]).strip(),
                "camera": str(row["camera"]).strip(),
                "transition": str(row["transition"]).strip(),
                **({"graphics": copy.deepcopy(row["graphics"])} if "graphics" in row else {}),
            }
        )
        cursor = end
    if any(row["end"] <= row["start"] for row in timeline):
        raise ValidationError("Shot duration is too short to represent in the timeline")
    return timeline


def graphics_direction(shot: dict[str, Any]) -> str:
    """One approved graphics description shared by both prompts and review pages."""
    labels = {"text": "文字", "icon": "图标", "effect": "特效"}
    return "; ".join(
        f"{labels[item['kind']]} {json.dumps(item['content'], ensure_ascii=False)}: {item['direction']}"
        for item in shot.get("graphics", [])
    )


def shot_direction(shot: dict[str, Any]) -> str:
    graphics = graphics_direction(shot)
    return (
        f"[{shot['start']:.2f}-{shot['end']:.2f}s] {shot['visual']}; "
        f"action: {shot['action']}; detail: {shot['product_detail']}; "
        f"camera: {shot['camera']}; transition: {shot['transition']}."
        + (f" Designed graphics: {graphics}." if graphics else "")
    )


def _build_source_assets(analysis: dict[str, Any], source_dir: Path) -> list[dict[str, str]]:
    """Freeze local product evidence without making it an Omni reference pack."""
    assets: list[dict[str, str]] = []
    seen_hashes: set[str] = set()
    for raw_path in analysis["source_images"]:
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            raise ValidationError("analysis.source_images paths must be absolute")
        path = path.resolve()
        if not path.is_file():
            raise ValidationError(f"Source product image is missing: {path}")
        suffix = validate_reference_image(path)
        digest = sha256_file(path)
        if digest in seen_hashes:
            raise ValidationError("analysis.source_images contains the same file more than once")
        seen_hashes.add(digest)
        source_dir.mkdir(parents=True, exist_ok=True)
        snapshot = source_dir / f"{digest}{suffix}"
        if snapshot.resolve() != path:
            shutil.copy2(path, snapshot)
        if sha256_file(snapshot) != digest:
            raise ValidationError("Product evidence changed while making the private snapshot")
        assets.append({"path": str(snapshot.resolve()), "original_path": str(path), "sha256": digest,
                       "usage": "imagegen_evidence_only", "pixel_sha256": image_pixel_hash(snapshot)})
    return assets


def clip_has_talent(plan: dict[str, Any], clip: dict[str, Any]) -> bool:
    return plan["talent_strategy"]["mode"] == "human_interaction" and bool(str(clip.get("talent_action") or "").strip())


def execution_roles(plan: dict[str, Any], clip: dict[str, Any]) -> list[str]:
    roles = clip.get("execution_reference_roles")
    if roles is None:
        roles = ["storyboard"]
        if len(clip.get("shots", [])) > 1:
            roles.append("product_master")
        if clip_has_talent(plan, clip) and plan["talent_strategy"]["framing"] != "hands_only":
            roles.append("talent")
    if (not isinstance(roles, list) or not 1 <= len(roles) <= 3
            or roles[0] != "storyboard"
            or any(not isinstance(role, str) or role not in {"storyboard", "product_master", "talent"} for role in roles)
            or len(set(roles)) != len(roles)):
        raise ValidationError("execution_reference_roles must start with storyboard, optionally product_master and talent")
    if "talent" in roles and not clip_has_talent(plan, clip):
        raise ValidationError("A product-only segment cannot reference talent")
    return list(roles)


def exclusive_project(method):
    """One state-changing command per project, including separate CLI processes."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with file_lock(self.project_dir / ".operation.lock", blocking=False):
            return method(self, *args, **kwargs)
    return wrapped


class VisualPromptBuilder:
    """Build shared anchors, storyboard prompts, and fixed-contract Omni prompts."""

    def __init__(self, analysis: dict[str, Any], plan: dict[str, Any]):
        self.analysis = analysis
        self.plan = plan
        self.aspect_ratio = str(plan["aspect_ratio"])
        self.frame_label_cn = "竖屏" if self.aspect_ratio == "9:16" else "横屏"
        self.frame_label_en = "portrait" if self.aspect_ratio == "9:16" else "landscape"

    def global_anchor(self, *, include_talent: bool = True) -> str:
        visible = "; ".join(str(item) for item in self.analysis["visible_features"])
        materials = ", ".join(str(item) for item in self.analysis["materials"])
        colors = ", ".join(str(item) for item in self.analysis["colors"])
        palette = self.plan["palette"]
        logo = str(self.analysis.get("logo_text") or "no invented logo text")
        return (
            f"PRODUCT ID LOCK: {self.analysis['product_type']}; exact silhouette, proportions, controls, "
            f"openings, label placement and geometry from the approved generated product master; "
            f"source-image facts: {visible}; "
            f"materials: {materials}; observed colors: {colors}; logo/text: {logo}. "
            f"GLOBAL COLOR LOCK: primary {palette['primary']}, secondary {palette['secondary']}, "
            f"rim light {palette['rim']}. GLOBAL ART DIRECTION: {self.plan['global_anchor']}. "
            f"{self.talent_anchor(include_talent=include_talent)}"
        )

    def product_master_prompt(self) -> str:
        """Build the clean internal identity asset used to generate the final storyboard."""
        visible = "; ".join(str(item) for item in self.analysis["visible_features"])
        materials = ", ".join(str(item) for item in self.analysis["materials"])
        colors = ", ".join(str(item) for item in self.analysis["colors"])
        logo = str(self.analysis.get("logo_text") or "no readable logo; do not invent one")
        input_roles = "; ".join(
            f"Image {index}: product identity evidence only"
            for index, _ in enumerate(self.analysis["source_images"], start=1)
        )
        return (
            "Use case: product-mockup\n"
            "Asset type: approved generated product master for downstream storyboard identity control.\n"
            f"Input images: {input_roles}. Use every input only to verify the same advertised product or verified set; never copy "
            "the source background, furniture, screenshot framing, compression artifacts or lighting.\n"
            "Primary request: generate one new professional studio product master. Choose the canvas and view "
            "to show the complete advertised product or verified product set, unobstructed and large enough "
            "to inspect, in a neutral premium studio. Do not constrain its canvas to the final video ratio. This is "
            "an identity anchor, not the final ad keyframe and not a storyboard.\n"
            f"Product facts to preserve: product type {self.analysis['product_type']}; visible structure {visible}; "
            f"materials {materials}; observed colors {colors}; logo/text {logo}.\n"
            "Composition/framing: choose a view supported by the source evidence and suitable for this product; "
            "preserve its silhouette, proportions, labels and all verified parts. Controls, openings, hinges or "
            "bases must be preserved only where actually present; do not invent unseen sides or parts.\n"
            "Lighting/mood: soft controlled studio key, readable rim separation and physically plausible reflections; "
            "no dramatic VFX, energy ring, particles, typography or environment story.\n"
            "Text: preserve only verified product labels or logo exactly where visible; add no advertising copy.\n"
            "Constraints: newly generated clean product image; exact product identity; no redesign, no extra parts, "
            "no missing parts or verified set components, no added items beyond the verified product/set, no person, no hands, no collage, no panels, no border, no "
            "watermark, no unrelated text. If a detail is unclear, keep it visually conservative rather than inventing it."
        )

    def talent_anchor(self, *, include_talent: bool = True) -> str:
        strategy = self.plan["talent_strategy"]
        if strategy["mode"] == "pure_product" or not include_talent:
            return (
                "CAST MODE LOCK: pure product mode; no person, no model, no face, no hands, "
                "no body parts and no human silhouette"
            )
        actions = "; ".join(str(item) for item in strategy["interaction_actions"])
        return (
            "CAST MODE LOCK: show the same clearly adult talent only in shots whose actions require them; "
            f"framing {strategy['framing']}; persona {strategy['persona']}; "
            f"wardrobe {strategy['wardrobe']}; grooming {strategy['grooming']}; "
            f"identity continuity {strategy['identity_anchor']}; approved interactions {actions}. "
            "Preserve face, hair, skin tone, body proportions, hands and outfit from the approved talent "
            "reference. Anatomically correct hands, natural product grip, no extra fingers, no duplicate person, "
            + ("Follow approved speech and lip-sync direction" if audio_routing.native(self.plan) else "no lip-sync and no speaking gesture")
        )

    def talent_reference_prompt(self) -> str | None:
        strategy = self.plan["talent_strategy"]
        if strategy["mode"] == "pure_product":
            return None
        palette = self.plan["palette"]
        framing = strategy["framing"]
        composition = {
            "hands_only": "adult hands and forearms with both hands fully visible",
            "partial_body": "single adult three-quarter portrait with both hands visible and unobstructed",
            "face_and_body": (
                "single adult full-body commercial portrait with face, both hands, outfit silhouette and footwear visible"
            ),
        }[framing]
        return (
            f"中文要求：生成一张{self.frame_label_cn} {self.aspect_ratio} 的全局成年人物设定参考图，只出现同一位成年人，"
            f"人物定位：{strategy['persona']}；服装：{strategy['wardrobe']}；妆造：{strategy['grooming']}；"
            f"身份锚点：{strategy['identity_anchor']}。双手结构清楚自然，不拿商品，不出现文字、Logo、分格或多人。\n"
            f"English identity reference: {composition}; clearly adult, matching the age in the approved persona, never teen or child. "
            f"Exact persona {strategy['persona']}; exact wardrobe {strategy['wardrobe']}; exact grooming "
            f"{strategy['grooming']}; identity lock {strategy['identity_anchor']}. Neutral uncluttered studio, "
            f"primary {palette['primary']}, secondary {palette['secondary']}, rim light {palette['rim']}; "
            "clean premium commercial lighting, realistic skin and hands, five correct fingers per hand, "
            "no product, no text, no logo, no grid, no duplicate person. This image is an identity reference, "
            "not a finished advertisement."
        )

    def storyboard_prompt(self, clip: dict[str, Any]) -> str:
        panels = int(clip["storyboard_panel_count"])
        if panels not in STORYBOARD_LAYOUTS:
            raise ValidationError("Storyboard panel count must be between 1 and 4")
        style = self.plan["style"]
        renderer = (
            "Octane Render, Unreal Engine 5, cinematic 3D product rendering, 8K detail language"
            if style != "cinematic_neo_noir"
            else "35mm commercial film texture, cinematic product macro, physically accurate materials"
        )
        ad_copy = " / ".join(str(item) for item in self.plan.get("ad_copy", [])) or "no ad copy"
        talent_action = str(clip.get("talent_action") or "不出现人物")
        shots = " ".join(f"Shot {row['index']}, purpose {row['purpose']}: {shot_direction(row)}" for row in clip["shots"])

        if panels == 1:
            layout_cn = (
                f"制作一张独立的{self.frame_label_cn} {self.aspect_ratio} 电影级干净分镜关键画面。只允许一个完整画面，"
                "不要分格、拼贴、边框、编号、时间码或说明文字。"
            )
        elif panels in {2, 3}:
            layout_cn = (
                f"制作一张包含 {panels} 个连续画面的干净视觉分镜图，采用单行从左到右的阅读顺序。"
                f"每格的形状和尺寸以清楚表达镜头为准，不要求机械套用成片的 {self.aspect_ratio} 比例；"
                f"但主体位置、留白和文字安全区必须能够自然适配最终{self.frame_label_cn} {self.aspect_ratio} 成片。"
                "各格之间使用清楚留白，不得重复构图。画内禁止编号、时间码、导演说明和旁白字幕；允许已批准的广告标题与品牌文字。"
            )
        else:
            layout_cn = (
                "制作一张包含 4 个连续画面的干净 2×2 视觉分镜图，阅读顺序固定为左上、右上、左下、右下。"
                f"每格比例以清楚表达镜头为准，不要求机械套用成片的 {self.aspect_ratio} 比例；"
                f"但主体位置、留白和文字安全区必须能够自然适配最终{self.frame_label_cn} {self.aspect_ratio} 成片。"
                "各格使用清楚留白并保持不同动作阶段；画内禁止编号、时间码、导演说明和旁白字幕；允许已批准的广告标题与品牌文字。"
            )
        return (
            f"中文要求：{layout_cn}这是第 {clip['index']} 段广告；分镜选择理由：{clip['storyboard_reason']}。"
            f"整条广告故事弧：{self.plan['story_arc']}。本段叙事任务：{clip['narrative_role']}；"
            f"入场衔接：{clip['transition_in']}。按时序执行：{shots} "
            f"每格必须用画面清楚表现对应任务、动作与商品细节，不得用说明文字代替。"
            f"画面演进总览：{clip['visual_progression']}；运镜总览：{clip['camera']}；"
            f"人物动作：{talent_action}；声音设计：{clip['sfx']}；转场终点：{clip['transition_out']}。"
            f"严格保持商品结构、Logo位置和材质一致。广告文字只允许：{ad_copy}。\n"
            f"Art direction: {self.global_anchor(include_talent=clip_has_talent(self.plan, clip))}. Main style: {STYLE_NAMES[style]}. "
            "This approved storyboard will be uploaded to Omni unchanged as one visual reference; "
            "the panels are sequential story beats, never simultaneous products. "
            "Build the designed advertising world, not the neutral product-master studio. Choose a representative action state that makes its trigger and motion legible; "
            "do not show every interaction already completed. Match the planned entry and exit states across clips. "
            "Show planned 3D typography, icons and VFX with clear depth and material. "
            "Keep printed branding attached and unchanged; logo-shaped VFX are separate scene elements. "
            "Use the effect hierarchy and camera intentions designed in the shots; preserve real product geometry "
            "while staging expressive graphics, scale, light and environmental motion around it. "
            "Keep product details, Logo and adult faces readable at key viewing moments. "
            f"{renderer} (art direction only, not an output specification). "
            "Premium international commercial art direction, precise lighting, no cheap template, no narration subtitles, "
            "no invented claims, no unrelated text, no malformed logo."
        )

    def omni_prompt(self, clip: dict[str, Any]) -> str:
        roles = execution_roles(self.plan, clip)
        panels = int(clip["storyboard_panel_count"])
        descriptions = {
            "storyboard": f"story order and composition, {panels} beats read {storyboard_reading_order(panels)}",
            "product_master": "product identity only: shape, material, Logo and label placement; not scene or camera",
            "talent": "adult identity and wardrobe only; not pose, scene or camera",
        }
        bindings = " ".join(f"Image{i}: {descriptions[role]}." for i, role in enumerate(roles, 1))
        timeline = " ".join(shot_direction(row) for row in clip["shots"])

        cast = ("In shots requiring talent, preserve the same clearly adult person, wardrobe and natural hands from "
                + (f"Image{roles.index('talent') + 1}" if "talent" in roles else "Image1")
                + ("; follow approved speech direction." if audio_routing.native(self.plan) else "; no speaking gestures.")) if clip_has_talent(self.plan, clip) else "Pure product; no person, face, hands or body parts."
        sequence = "In a single unbroken scene; no scene cuts." if panels == 1 else f"Follow exactly {panels} sequential shots in timecode order."
        action_end = float(clip["shots"][-1]["end"])
        copy_text = " / ".join(self.plan.get("ad_copy", [])) or "none"
        prompt = (
            f"{bindings} Create a premium {self.aspect_ratio} commercial. References are not literal initial frames. "
            "Image1 controls the story; identity references must not override its compositions. "
            f"Recompose moving shots for {self.aspect_ratio}; panel shapes do not constrain output. "
            "Create moving commercial cinematography with the planned action, spatial parallax and graphic animation, "
            "not a pan across still reference panels. "
            f"{sequence} Enter: {clip['transition_in']}. {timeline} "
            f"Keep product geometry, materials and Logo consistent; no invented parts or claims. {cast} "
            f"Art direction: {STYLE_NAMES[self.plan['style']]}; {self.plan['global_anchor']}. "
            "Only use the effects designed in these shots; protect product details, Logo and faces. "
            f"[{action_end:.2f}-10.00s] Continue the exit state: {clip['transition_out']}; "
            f"keep motion coherent through the local cut at {clip['keep_duration']}s. "
            f"Approved advertising copy inventory: {copy_text}; use each item only where the shot design places it, "
            "not on every shot. Planned 3D lettering, icons and VFX are intentional scene elements, not subtitles "
            "or forbidden layout. Preserve verified packaging text. "
            "No narration subtitles, unrelated text, watermark, collage, split screen, storyboard grid, "
            "gutters, panel borders, shot numbers or timecodes in the video. "
            + audio_routing.prompt(self.plan, clip)
        )

        mentioned = {int(n) for n in re.findall(r"\bImage(\d+)\b", prompt)}
        if any(n < 1 or n > len(roles) for n in mentioned) or "<IMAGE_REF_" in prompt:
            raise ValidationError("Video prompt contains an unbound or unsupported image reference")
        return prompt


class AdOrchestrator:
    """Manage approvals, references, paid generation, recovery, and delivery."""

    def __init__(
        self,
        project_dir: Path,
        *,
        key_loader: Callable[[str, str], str] | None = None,
        publisher_cls: type[TemporaryPublisher] = TemporaryPublisher,
        omni_factory: Callable[..., OmniClient] = OmniClient,
        fallback_factory: Callable[..., CangyuanClient] = CangyuanClient,
        minimax_factory: Callable[..., MiniMaxClient] = MiniMaxClient,
    ):
        self.project_dir = project_dir.expanduser().resolve()
        self.state_path = self.project_dir / "project.json"
        self.ledger = TaskLedger(self.project_dir / "requests" / "task-ledger.json")
        self.key_loader = key_loader or Keychain.load
        self.publisher_cls = publisher_cls
        self.omni_factory = omni_factory
        self.fallback_factory = fallback_factory
        self.minimax_factory = minimax_factory

    @classmethod
    def create(cls, project_id: str, output_root: Path) -> "AdOrchestrator":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", project_id):
            raise ValidationError("project-id must use letters, numbers, dot, underscore, or hyphen")
        return cls(output_root.expanduser().resolve() / project_id)

    @exclusive_project
    def prepare(
        self,
        analysis: dict[str, Any],
        plan: dict[str, Any],
        *,
        target_duration: float | None = None,
        parent_project: Path | None = None,
        replace_clip: int | None = None,
        video_provider: str = "auto",
        model: str | None = None,
        strategy: str | None = None,
        output_resolution: str | None = None,
        validation_run: bool = False,
    ) -> dict[str, Any]:
        if self.ledger.records():
            raise ValidationError("Paid history is immutable; resume the full project or prepare a bound replacement with parent-project and replace-clip")
        if self.state_path.is_file() and self.load()["state"] != "awaiting_plan_approval":
            raise ValidationError("This project already passed the plan stage; use a new project-id for revisions")
        if not isinstance(plan, dict):
            raise ValidationError("plan must be an object")
        if video_provider not in {"auto", "wxart", "cangyuan"}:
            raise ValidationError("video-provider must be auto, wxart, or cangyuan")
        plan = copy.deepcopy(plan)
        plan.setdefault("recovery_policy", {"max_video_retries_per_clip": 1})
        if (parent_project is None) != (replace_clip is None):
            raise ValidationError("parent-project and replace-clip must be supplied together")
        if parent_project is not None:
            parent = AdOrchestrator(parent_project)
            if parent.project_dir == self.project_dir:
                raise ValidationError("A replacement must have a separate immutable record")
            original = parent.load()
            parent._validate_generation_approval(original, allowed_states={"failed", "generating", "awaiting_manual_review"})
            if original["plan"].get("replacement_for"):
                raise ValidationError("Replacement must point directly to the full advertisement")
            target_clip = next((c for c in original["clips"] if c["index"] == replace_clip), None)
            if (not target_clip or target_duration != target_clip["keep_duration"]
                    or plan.get("aspect_ratio") != original["target"]["aspect_ratio"]):
                raise ValidationError("Replacement must preserve parent clip duration and aspect ratio")
            original_hashes = {r["sha256"] for r in original["source_assets"]}
            if {sha256_file(Path(p)) for p in analysis.get("source_images", [])} != original_hashes:
                raise ValidationError("Replacement must reuse the original product evidence")
            if plan.get("narration", {}).get("enabled"):
                raise ValidationError("Replacement uses the parent full-length narration; do not synthesize a segment voice")
            plan["replacement_for"] = {"project": str(parent.project_dir), "clip_index": replace_clip,
                "parent_approval": original["approvals"]["references"], "target": original["target"],
                "narration": original["plan"]["narration"]}
        elif plan.get("replacement_for"):
            raise ValidationError("Use parent-project and replace-clip to bind a replacement")
        if parent_project is not None:
            plan["audio"] = copy.deepcopy(original["plan"].get("audio") or {"mode": "external" if original["plan"]["narration"]["enabled"] else "video", "speech": False})
            if plan["audio"]["mode"] == "external":
                plan["audio"]["user_requested_external"] = True
        audio_routing.normalize_plan(plan)
        _validate_analysis(analysis)
        if analysis.get("source_mode") == "authorized_fictional_concept":
            if "概念演示" not in plan.get("ad_copy", []):
                raise ValidationError("Fictional concepts must disclose 概念演示 in approved ad_copy")
        config = caps.read_config()
        if parent_project is not None:
            model = model or original['provider_contract']['model']
            if video_provider == 'auto':
                video_provider = original['provider_contract']['provider']
            if output_resolution and output_resolution != original['target']['resolution']:
                raise ValidationError('Replacement must preserve the parent output resolution')
            output_resolution = original['target']['resolution']
        cap = caps.route(model or config.get('model','omni'), video_provider if video_provider != 'auto' else config.get('provider','auto'))
        if cap['family'] != 'omni':
            video_provider = 'cangyuan'
        output_resolution = output_resolution or config.get('output_resolution','720p')
        source_resolution = caps.source_resolution(cap, output_resolution)
        cap.update(execution_resolution=source_resolution, aspect_ratio=plan.get('aspect_ratio'))
        plan, keeps = caps.prepare_timeline(plan, target_duration, cap,
            strategy or config.get('strategy','auto'), validation_run)
        audio_routing.normalize_plan(plan)
        target_duration = plan['target_duration']
        if parent_project is not None and len(plan["clips"]) != 1:
            raise ValidationError("A replacement binds one parent generation segment; use one approved request, not a multi-clip child")
        _validate_plan(plan, keeps)
        narration_window(plan, target_duration)
        for clip in plan["clips"]:
            clip["execution_reference_roles"] = execution_roles(plan, clip)
            clip.setdefault("execution_reference_reason", "分镜控制动作与构图；所选身份图补充格内不易辨认的商品或成年人物细节")
        planned = plan.get("clips", [])
        analysis = copy.deepcopy(analysis)
        source_assets = _build_source_assets(analysis, self.project_dir / "private" / "source-images")
        analysis["source_images"] = [row["path"] for row in source_assets]
        aspect_ratio = str(plan["aspect_ratio"])
        target_width, target_height = SUPPORTED_ASPECT_RATIOS[aspect_ratio]
        if output_resolution == "1080p":
            target_width, target_height = target_width * 3 // 2, target_height * 3 // 2
        builder = VisualPromptBuilder(analysis, plan)
        clips: list[dict[str, Any]] = []
        global_cursor = 0.0
        for index, keep in enumerate(keeps, start=1):
            source = next(row for row in plan["clips"] if int(row["index"]) == index)
            try:
                margin = float(source.get("tail_margin", min(0.8, float(keep) / 2)))
            except (TypeError, ValueError) as exc:
                raise ValidationError("tail_margin must be a number") from exc
            if not math.isfinite(margin) or not 0 < margin < keep:
                raise ValidationError("tail_margin must leave a positive usable action interval")
            action_end = float(keep) - margin
            shots = materialize_shot_timeline(source["shots"], action_end)
            for shot, original_shot in zip(shots, source["shots"]):
                shot["shot_id"] = original_shot["shot_id"]
            panel_count = len(shots)
            clip = {
                "index": index,
                "upstream_duration": source["upstream_duration"],
                "keep_duration": keep,
                "global_start": source["global_start"],
                "global_end": source["global_end"],
                "execution_strategy": source["execution_strategy"],
                "keep_frames": source["keep_frames"],
                "overlap_frames": source["overlap_frames"],
                "next_overlap_frames": planned[index]["overlap_frames"] if index < len(planned) else 0,
                "trim_start": source["trim_start"],
                "continuity_from": source.get("continuity_from"),
                "entry_state": source.get("entry_state", source["shots"][0]["visual"]),
                "exit_state": source.get("exit_state", source["shots"][-1]["action"]),
                "storyboard_layout": STORYBOARD_LAYOUTS[panel_count][0],
                "storyboard_panel_count": panel_count,
                "omni_reference_strategy": "approved_role_bindings",
                "execution_reference_roles": source["execution_reference_roles"],
                "execution_reference_reason": source["execution_reference_reason"],
                "tail_margin": margin,
                "storyboard_reading_order": storyboard_reading_order(panel_count),
                "storyboard_reason": source["storyboard_reason"],
                "narrative_role": source["narrative_role"],
                "shots": shots,
                "visual_progression": source["visual_progression"],
                "camera": source["camera"],
                "transition_in": source["transition_in"],
                "transition_out": source["transition_out"],
                "talent_action": str(source.get("talent_action") or ""),
                "sfx": source["sfx"],
                "audio": copy.deepcopy(source.get("audio", {})),
            }
            clip["storyboard_prompt"] = builder.storyboard_prompt(clip)
            if cap["family"] != "omni":
                clip["storyboard_prompt"] = clip["storyboard_prompt"].replace("uploaded to Omni", "uploaded to the selected video model")
            clip["omni_prompt"] = builder.omni_prompt(clip)
            clip["video_prompt"] = advanced.video_prompt(builder, clip, cap)
            if clip["execution_strategy"] not in {"full_storyboard", "per_shot"}:
                clip["frame_prompts"] = advanced.frame_prompts(builder, clip)
            clips.append(clip)
            global_cursor += keep
        narration = plan["narration"]
        human_mode = plan["talent_strategy"]["mode"] == "human_interaction"
        generation_order = ["product_master"]
        if human_mode:
            generation_order.append("talent")
        generation_order.extend(f"storyboard:{clip['index']}" for clip in clips)
        direct_cangyuan = video_provider == "cangyuan"
        provider_contract = {
            "model": VIDEO_MODEL_ID,
            "provider": "cangyuan" if direct_cangyuan else "wxart",
            "provider_model": "omni-fast-no-water" if direct_cangyuan else "omni-flash",
            "selection": "explicit_user_authorization" if video_provider != "auto" else "automatic_primary_with_safe_fallback",
            "mode": "ref",
            "upstream_duration": 10,
            "aspect_ratio": aspect_ratio,
            "resolution": "720p",
            "watermark": False,
            "max_reference_images": 3,
            "max_concurrency": 2,
            "fallback": {
                "provider": "cangyuan",
                "base_url": "https://ai.cangyuansuanli.cn",
                "model": "omni-fast-no-water",
                "enabled": video_provider == "auto",
                "trigger": "primary health check failure or deterministic primary rejection before task creation",
            },
        }
        chunks = advanced.plan_narration(plan, target_duration)
        provider_contract.update(route_id=cap['route_id'], revision=cap['revision'], capabilities=cap,
            model=VIDEO_MODEL_ID if cap['family']=='omni' else cap['model'], provider=cap['provider'],
            provider_model=cap['model'], resolution=source_resolution, strategy_policy=strategy or config.get('strategy','auto'),
            budget=caps.budget_contract(plan, len(clips), len(chunks)), narration_chunks=chunks,
            audio=copy.deepcopy(plan['audio']))
        if cap['family'] != 'omni':
            provider_contract.update(upstream_duration=None, mode='reference', max_reference_images=cap['images'])
            provider_contract['fallback']['enabled'] = False
        advanced.configure_asset_plan(generation_order, clips)
        state = {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "project_id": self.project_dir.name,
            "state": "awaiting_plan_approval",
            "target": {
                "duration": target_duration,
                "aspect_ratio": aspect_ratio,
                "aspect_ratio_reason": plan["aspect_ratio_reason"],
                "width": target_width,
                "height": target_height,
                "resolution": output_resolution,
                "frames": caps.frames(target_duration),
                "requested_duration": plan["requested_duration"],
            },
            "provider_contract": provider_contract,
            "analysis": analysis,
            "source_assets": source_assets,
            "plan": plan,
            "global_anchor": builder.global_anchor(),
            "product_master_prompt": builder.product_master_prompt(),
            "talent_reference_prompt": builder.talent_reference_prompt(),
            "reference_asset_plan": {
                "source_usage": "imagegen_evidence_only_never_omni",
                "generation_order": generation_order,
                "minimum_imagegen_calls": len(generation_order),
            },
            "clips": clips,
            "paid_counts": {
                "omni": len(clips),
                "minimax": len(chunks),
            },
            "references": [],
            "reference_sets": {},
            "approvals": {},
            "artifacts": {},
        }
        if plan.get("replacement_for"):
            reusable = [r for r in original["references"] if r["role"] == "product_master" or
                        (r["role"] == "talent" and plan["talent_strategy"] == original["plan"]["talent_strategy"])]
            reused_roles = {r["role"] for r in reusable}
            state["reference_asset_plan"]["reusable_assets"] = reusable
            state["reference_asset_plan"]["generation_order"] = [role for role in generation_order if role not in reused_roles]
            state["reference_asset_plan"]["minimum_imagegen_calls"] = len(state["reference_asset_plan"]["generation_order"])
        image_config = image_api.read_config()
        # Snapshot in the existing plan contract: later configuration changes cannot
        # silently change the approved model, service, size or paid-call ceiling.
        state["reference_asset_plan"]["image_config"] = image_config
        image_attempts = image_config.get("max_attempts_per_asset", 2)
        state["reference_asset_plan"]["maximum_image_calls"] = (
            state["reference_asset_plan"]["minimum_imagegen_calls"] * image_attempts)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        for name in ("references", "requests", "audio", "clips/raw", "clips/normalized", "final", "qa/frames"):
            (self.project_dir / name).mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.state_path, state)
        (self.project_dir / "plan.md").write_text(_render_plan(state), encoding="utf-8")
        return state

    @exclusive_project
    def approve_plan(self) -> dict[str, Any]:
        state = self.load()
        if state["state"] != "awaiting_plan_approval":
            raise ValidationError("Plan can only be approved from awaiting_plan_approval")
        self._validate_source_files(state)
        state["approvals"] = {"plan": canonical_hash(_plan_contract(state))}
        state["state"] = "awaiting_reference_approval"
        self.save(state)
        return state

    @exclusive_project
    def register_references(self, manifest: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        self._validate_plan_approval(state)
        self._validate_source_files(state)
        if state["state"] != "awaiting_reference_approval":
            raise ValidationError("References can only be registered after plan approval")
        rows = manifest.get("references")
        if not isinstance(rows, list) or not rows:
            raise ValidationError("Reference manifest must contain a non-empty references list")
        for row in rows:
            if not isinstance(row, dict):
                raise ValidationError("Each reference must be an object")
            image_config = state["reference_asset_plan"].get("image_config", {"provider": "builtin"})
            reused = any(r.get("generation_receipt") == row.get("generation_receipt")
                         and r["role"] == row.get("role")
                         and r["sha256"] == sha256_file(Path(row["path"]))
                         for r in state["reference_asset_plan"].get("reusable_assets", []))
            if (image_config["provider"] != "builtin" and row.get("origin") == "codex_imagegen"
                    and not reused):
                raise ValidationError("This plan approved an external image API; do not silently switch image tools")
            if row.get("origin") == "external_image_api":
                if not reused:
                    image_api.verify_receipt(self.project_dir, row)
        registered = [self._normalize_reference_row(row, order) for order, row in enumerate(rows, start=1)]
        reference_sets = self._validate_reference_pack(state, registered)
        state["references"] = registered
        state["reference_sets"] = reference_sets
        report_path = self.project_dir / "reference-approval.md"
        state.setdefault("artifacts", {})["reference_approval"] = str(report_path)
        state["approvals"].pop("references", None)
        state["state"] = "awaiting_reference_approval"
        self.save(state)
        atomic_write_json(self.project_dir / "references" / "manifest.json", {"references": registered})
        report_path.write_text(_render_reference_approval(state), encoding="utf-8")
        return state

    def _normalize_reference_row(self, row: dict[str, Any], order: int) -> dict[str, Any]:
        if not isinstance(row, dict):
            raise ValidationError("Each reference must be an object")
        role = row.get("role")
        if role in {"keyframe", "start_frame", "end_frame"}:
            return advanced.normalize_reference(self, row, order)
        if role == "product":
            raise ValidationError(
                "Raw product references are forbidden; generate and register a product_master instead"
            )
        if role not in {"product_master", "storyboard", "style", "talent"}:
            raise ValidationError(f"Unsupported reference role: {role}")
        path = Path(str(row.get("path") or "")).expanduser()
        if not path.is_absolute():
            raise ValidationError("Reference paths must be absolute")
        path = path.resolve()
        if not path.is_file():
            raise ValidationError(f"Reference file is missing: {path}")
        try:
            path.relative_to((self.project_dir / "references").resolve())
        except ValueError as exc:
            raise ValidationError("Reference files must be copied into this project's references directory") from exc
        validate_reference_image(path)
        image_info = probe_media(path)
        width = int(image_info.get("width") or 0)
        height = int(image_info.get("height") or 0)
        if width <= 0 or height <= 0:
            raise ValidationError(f"Reference image has no readable dimensions: {path}")
        origin = str(row.get("origin") or "")
        if role in {"product_master", "storyboard"} and origin not in {"codex_imagegen", "external_image_api"}:
            raise ValidationError(f"{role} must be a newly generated image asset")
        if role in {"style", "talent"} and origin not in {"codex_imagegen", "external_image_api", "user_provided"}:
            raise ValidationError(f"{role} origin must be generated or user_provided")
        clip_index = row.get("clip_index")
        if role == "storyboard":
            try:
                clip_index = int(clip_index)
            except (TypeError, ValueError) as exc:
                raise ValidationError("Storyboard references require clip_index") from exc
            if (
                row.get("clean_for_video") is not True
                or row.get("panel_order_verified") is not True
                or row.get("distinct_panels_verified") is not True
            ):
                raise ValidationError(
                    "Storyboard must be visually checked and declare clean_for_video=true, "
                    "panel_order_verified=true, and distinct_panels_verified=true"
                )
        elif clip_index is not None:
            raise ValidationError(f"{role} reference must be global and omit clip_index")
        if role == "product_master" and row.get("identity_verified") is not True:
            raise ValidationError("product_master must be visually checked and declare identity_verified=true")
        return {
            "order": order,
            "role": role,
            "clip_index": clip_index,
            "path": str(path),
            "origin": origin,
            **({"generation_receipt": row.get("generation_receipt")} if origin == "external_image_api" else {}),
            "sha256": sha256_file(path),
            "pixel_sha256": image_pixel_hash(path),
            "width": width,
            "height": height,
            "derived_from_source_hashes": row.get("derived_from_source_hashes"),
            "derived_from_product_master_sha256": row.get("derived_from_product_master_sha256"),
            "derived_from_talent_sha256": row.get("derived_from_talent_sha256"),
            "panel_count": row.get("panel_count"),
            "identity_verified": row.get("identity_verified") if role == "product_master" else None,
            "clean_for_video": row.get("clean_for_video") if role == "storyboard" else None,
            "panel_order_verified": row.get("panel_order_verified") if role == "storyboard" else None,
            "distinct_panels_verified": row.get("distinct_panels_verified") if role == "storyboard" else None,
        }

    def _validate_reference_pack(
        self, state: dict[str, Any], registered: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        if state.get("schema_version", 0) >= 9 and any(c["execution_strategy"] not in {"full_storyboard", "per_shot"} for c in state["clips"]):
            return advanced.validate_references(self, state, registered)
        sha_groups: dict[str, list[dict[str, Any]]] = {}
        for row in registered:
            sha_groups.setdefault(row["sha256"], []).append(row)
        duplicate_groups = [group for group in sha_groups.values() if len(group) > 1]
        if duplicate_groups:
            labels = ", ".join(
                f"{row['role']}:{row.get('clip_index') or 'global'}"
                for row in duplicate_groups[0]
            )
            raise ValidationError(f"Reference files are exact duplicates ({labels}); keep only distinct approved assets")
        source_hashes = sorted(str(row["sha256"]) for row in state["source_assets"])
        source_pixels = {row["pixel_sha256"] for row in state["source_assets"]}
        for reference in registered:
            is_source_copy = reference["sha256"] in source_hashes or reference["pixel_sha256"] in source_pixels
            if is_source_copy:
                if reference["role"] == "product_master":
                    raise ValidationError(
                        "product_master is the raw source image or an identical-pixel copy; generate a new clean professional master"
                    )
                raise ValidationError(
                    f"User source images cannot be registered as {reference['role']} or sent to Omni"
                )
        products = [row for row in registered if row["role"] == "product_master"]
        styles = [row for row in registered if row["role"] == "style"]
        talents = [row for row in registered if row["role"] == "talent"]
        all_storyboards = [row for row in registered if row["role"] == "storyboard"]
        for left_index, left in enumerate(all_storyboards):
            for right in all_storyboards[left_index + 1 :]:
                if left["pixel_sha256"] == right["pixel_sha256"]:
                    raise ValidationError("Storyboard references contain identical decoded pixels; keep distinct approved assets")
        if len(products) != 1:
            raise ValidationError("Exactly one global generated product_master reference is required")
        product_master = products[0]
        declared_source_hashes = product_master.get("derived_from_source_hashes")
        if not isinstance(declared_source_hashes, list) or sorted(map(str, declared_source_hashes)) != source_hashes:
            raise ValidationError("product_master must declare every frozen source-image hash used by imagegen")
        if len(styles) > 1:
            raise ValidationError("At most one global style reference is allowed")
        if len(talents) > 1:
            raise ValidationError("At most one global talent reference is allowed")
        talent_mode = state["plan"]["talent_strategy"]["mode"]
        if talent_mode == "human_interaction":
            if len(talents) != 1:
                raise ValidationError("Human interaction mode requires exactly one global talent reference")
            if styles:
                raise ValidationError(
                    "Human interaction mode must encode style in the storyboard instead of adding a style asset"
                )
        else:
            if talents:
                raise ValidationError("Pure product mode must not include a talent reference")
        reference_sets: dict[str, list[dict[str, Any]]] = {}
        for clip in state["clips"]:
            index = clip["index"]
            storyboards = [
                row for row in registered if row["role"] == "storyboard" and row["clip_index"] == index
            ]
            if len(storyboards) != 1:
                raise ValidationError(f"Clip {index} requires exactly one storyboard reference")
            storyboard = storyboards[0]
            if clip_has_talent(state["plan"], clip) and storyboard.get("derived_from_talent_sha256") != talents[0]["sha256"]:
                raise ValidationError(f"Clip {index} storyboard must declare the actual talent reference hash used by imagegen")
            if storyboard.get("derived_from_product_master_sha256") != product_master["sha256"]:
                raise ValidationError(
                    f"Clip {index} storyboard must be generated from the approved product_master"
                )
            if product_master["pixel_sha256"] == storyboard["pixel_sha256"]:
                raise ValidationError("Storyboard and product_master contain identical decoded pixels")
            try:
                declared_panels = int(storyboard.get("panel_count"))
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"Clip {index} storyboard must declare panel_count") from exc
            if declared_panels != int(clip["storyboard_panel_count"]):
                raise ValidationError(
                    f"Clip {index} storyboard panel_count does not match the approved plan"
                )
            assets_by_role = {"storyboard": storyboard, "product_master": product_master}
            if talents:
                assets_by_role["talent"] = talents[0]
            reference_sets[str(index)] = [
                {**assets_by_role[role], "order": position,
                 "role": "storyboard_reference" if role == "storyboard" else role,
                 "reference_strategy": "approved_role_bindings",
                 "reading_order": storyboard_reading_order(declared_panels) if role == "storyboard" else None}
                for position, role in enumerate(execution_roles(state["plan"], clip), 1)
            ]
        unexpected = [
            row for row in registered
            if row["role"] == "storyboard" and str(row["clip_index"]) not in reference_sets
        ]
        if unexpected:
            raise ValidationError("Reference manifest contains a storyboard for an unknown clip")
        return reference_sets

    @exclusive_project
    def approve_references(self) -> dict[str, Any]:
        state = self.load()
        self._validate_plan_approval(state)
        self._validate_reference_files(state)
        if state["state"] != "awaiting_reference_approval" or not state["references"]:
            raise ValidationError("Registered references are required before approval")
        state["approvals"]["references"] = canonical_hash(_reference_contract(state))
        state["approvals"]["paid_counts"] = canonical_hash(state["paid_counts"])
        state["state"] = "approved_for_generation"
        self.save(state)
        return state

    def preflight(self, *, verify_tunnel: bool = False) -> dict[str, Any]:
        state = self.load()
        self._validate_generation_approval(state)
        report = self._preflight_core(state)
        if verify_tunnel:
            unique = _unique_approved_files(state["reference_sets"])
            with self.publisher_cls(unique, evidence_dir=self.project_dir / "requests" / "publication") as publisher:
                assets = publisher.publish()
            report["temporary_https"] = {
                "verified": True,
                "asset_count": len(assets),
                "hashes": [asset.sha256 for asset in assets],
            }
        else:
            report["temporary_https"] = {"verified": False, "reason": "skipped"}
        atomic_write_json(self.project_dir / "preflight.json", report)
        return report

    @exclusive_project
    def produce(self) -> dict[str, Any]:
        return self._execute_generation(resuming=False)

    @exclusive_project
    def resume(self) -> dict[str, Any]:
        return self._execute_generation(resuming=True)

    def _cached_narration(self, state: dict[str, Any]) -> Path | None:
        narration = state["plan"]["narration"]
        if not narration["enabled"]:
            return None
        if state.get("schema_version", 0) >= 9 and bool(state["plan"]["narration"].get("chapters")):
            return advanced.cached_narration(self, state)
        path = self.project_dir / "audio" / "narration.mp3"
        request_hash = canonical_hash(MiniMaxClient.build_payload(
            narration["text"], narration["voice_id"], speed=float(narration.get("speed", 1))))
        if path.is_file() and any(r.get("request_hash") == request_hash and r.get("provider") == "minimax" and r.get("event") == "completed"
                and r.get("sha256") == sha256_file(path) for r in self.ledger.records()):
            return path
        return None

    def _execute_generation(self, *, resuming: bool) -> dict[str, Any]:
        state = self.load()
        allowed = {"generating", "submission_unknown", "failed", "awaiting_manual_review", "segment_ready"} if resuming else {
            "approved_for_generation", "generating", "submission_unknown", "failed"}
        self._validate_generation_approval(state, allowed_states=allowed)
        if state.get("state") == "segment_ready":
            return self._return_to_parent(state)
        if not resuming and state["schema_version"] not in {8, 9, PROJECT_SCHEMA_VERSION}:
            raise ValidationError("Legacy projects support resume only")
        # Old approvals did not include automatic replacements: their resume remains GET-only.
        allow_paid = state["schema_version"] in {8, 9, PROJECT_SCHEMA_VERSION} and (
            state["state"] == "approved_for_generation" or bool(state["plan"].get("recovery_policy")))
        if not allow_paid and not _merged_omni_tasks(self.ledger.records()):
            raise ValidationError("No known Omni task_id exists; preserve this project for provider review")
        self.ledger.schema_version = state["schema_version"]
        self.ledger.budget = state["provider_contract"].get("budget")
        state["state"] = "generating"
        state.pop("last_error", None)
        self.save(state)
        preflight: dict[str, Any] = {}

        def check_runtime() -> dict[str, Any]:
            if not preflight:
                preflight.update(self._preflight_core(state))
                atomic_write_json(self.project_dir / "preflight-production.json",
                    {k: v for k, v in preflight.items() if k != "system_voices"})
            return preflight

        try:
            narration_path = self._cached_narration(state)
            if state["plan"]["narration"]["enabled"] and narration_path is None:
                if (not allow_paid or (not state["plan"]["narration"].get("chapters") and any(r.get("provider") == "minimax" for r in self.ledger.records()))):
                    raise ValidationError("Approved narration is missing or changed; resume cannot create paid replacement audio")
                narration_path = self._generate_narration(state, check_runtime())
            if narration_path:
                info = probe_media(narration_path)
                if not info["has_audio"] or info["duration"] > narration_window(state["plan"], state["target"]["duration"])[1] + 0.10:
                    raise ValidationError("Narration is invalid or longer than the approved duration; Omni was not started")
            with ExitStack() as stack:
                publisher = None
                public: dict[str, str] = {}

                def ensure_public() -> dict[str, str]:
                    nonlocal publisher
                    if not allow_paid:
                        raise PaidRequestBlocked("This approval permits known-task queries only")
                    check_runtime()
                    if publisher is None:
                        publisher = stack.enter_context(self.publisher_cls(
                            _unique_approved_files(state["reference_sets"]),
                            evidence_dir=self.project_dir / "requests" / "publication"))
                        public.update({str(Path(a.source).resolve()): a.url for a in publisher.publish()})
                    publisher.health_check()
                    return public

                raw = self._generate_video_batches(state, {}, ensure_public=ensure_public, allow_paid=allow_paid)
            return self._assemble(state, raw, narration_path)
        except (ProviderError, ValidationError, PaidRequestBlocked, SubmissionUnknown) as exc:
            state = self.load()
            state["state"] = "segment_ready" if state["state"] == "segment_ready" else ("submission_unknown" if isinstance(exc, SubmissionUnknown) else "failed")
            state["last_error"] = {"type": type(exc).__name__, "message": str(exc),
                "recovery": "Continue the same project; preserve the approved plan, references and completed clips"}
            self.save(state)
            raise
        except Exception as exc:
            state = self.load()
            state["state"] = "segment_ready" if state["state"] == "segment_ready" else "failed"
            state["last_error"] = {"type": type(exc).__name__,
                "message": "Internal interruption; resume this project instead of replanning or regenerating references"}
            self.save(state)
            raise

    def _preflight_core(self, state: dict[str, Any]) -> dict[str, Any]:
        require_media_tools()
        if not shutil.which("cloudflared"):
            raise ValidationError("cloudflared is required")
        direct_cangyuan = state["provider_contract"].get("provider") == "cangyuan"
        omni_key = None
        cangyuan_key = None
        if direct_cangyuan:
            cangyuan_key = self.key_loader(CANGYUAN_KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        else:
            omni_key = self.key_loader(OMNI_KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            try:
                cangyuan_key = self.key_loader(CANGYUAN_KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            except ValidationError:
                cangyuan_key = None
        execution_rows = [row for rows in state["reference_sets"].values() for row in rows]
        report: dict[str, Any] = {
            "status": "pass",
            "state": state["state"],
            "model": state["provider_contract"],
            "paid_counts": state["paid_counts"],
            "approved_generated_asset_count": len(state["references"]),
            "omni_execution_reference_count": len(execution_rows),
            "omni_reference_sets": {
                clip_index: [
                    {
                        "position": position,
                        "role": row["role"],
                        "shot_index": row.get("shot_index"),
                        "sha256": row["sha256"],
                        "width": row["width"],
                        "height": row["height"],
                    }
                    for position, row in enumerate(rows, start=1)
                ]
                for clip_index, rows in state["reference_sets"].items()
            },
            "omni_key_present": bool(omni_key),
            "cangyuan_fallback_key_present": bool(cangyuan_key),
            "minimax_key_present": False,
        }
        narration = state["plan"]["narration"]
        if narration["enabled"] and self._cached_narration(state) is None:
            minimax_key = self.key_loader(MINIMAX_KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            voices = self.minimax_factory(minimax_key, self.ledger).list_system_voices()
            voice = next((row for row in voices if str(row.get("voice_id")) == narration["voice_id"]), None)
            if not voice:
                raise ValidationError("Approved MiniMax voice is no longer in system_voice; re-approve the plan")
            report["minimax_key_present"] = True
            report["verified_system_voice"] = {
                "voice_id": voice.get("voice_id"),
                "voice_name": voice.get("voice_name"),
            }
            report["system_voices"] = voices
        return report

    def _generate_narration(self, state: dict[str, Any], preflight: dict[str, Any]) -> Path | None:
        narration = state["plan"]["narration"]
        if not narration["enabled"]:
            return None
        if bool(state["plan"]["narration"].get("chapters")):
            return advanced.generate_narration(self, state, preflight)
        output = self.project_dir / "audio" / "narration.mp3"
        payload = MiniMaxClient.build_payload(
            narration["text"], narration["voice_id"], speed=float(narration.get("speed", 1.0))
        )
        request_hash = canonical_hash(payload)
        completed = next(
            (
                row for row in reversed(self.ledger.records())
                if row.get("provider") == "minimax"
                and row.get("event") == "completed"
                and row.get("request_hash") == request_hash
            ),
            None,
        )
        if completed and output.is_file() and sha256_file(output) == completed.get("sha256"):
            return output
        minimax_key = self.key_loader(MINIMAX_KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        client = self.minimax_factory(minimax_key, self.ledger)
        return client.synthesize(
            narration["text"], narration["voice_id"], output,
            verified_system_voices=preflight["system_voices"],
            speed=float(narration.get("speed", 1.0)),
        )

    def _generate_video_batches(
        self, state: dict[str, Any], public_by_source: dict[str, str], *,
        health_check: Callable[[], None] | None = None,
        ensure_public: Callable[[], dict[str, str]] | None = None,
        allow_paid: bool = True,
    ) -> list[Path]:
        if state["provider_contract"].get("capabilities", {}).get("family", "omni") != "omni":
            return advanced.generate_videos(self, state, ensure_public, allow_paid)
        direct_cangyuan = state["provider_contract"].get("provider") == "cangyuan"
        primary: OmniClient | None = None
        fallback: CangyuanClient | None = None
        active_client: OmniClient | CangyuanClient | None = None
        failover_enabled = not direct_cangyuan and self.omni_factory is OmniClient
        retry_limit = state["plan"].get("recovery_policy", {}).get("max_video_retries_per_clip", 0)
        replacements = self._replacement_sources(state)
        outputs: dict[int, Path] = {i: Path(r["path"]) for i, r in replacements.items()}

        def get_primary() -> OmniClient:
            nonlocal primary
            if primary is None:
                primary = self.omni_factory(
                    self.key_loader(OMNI_KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT), self.ledger
                )
            return primary

        def get_fallback() -> CangyuanClient:
            nonlocal fallback
            if fallback is None:
                fallback = self.fallback_factory(
                    self.key_loader(CANGYUAN_KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT), self.ledger
                )
            return fallback

        def choose_client() -> OmniClient | CangyuanClient:
            nonlocal active_client
            if active_client is not None:
                return active_client
            if direct_cangyuan:
                active_client = get_fallback()
                active_client.check_available()
                if not any(
                    row.get("event") == "fallback_selected"
                    and row.get("reason") == "explicit_user_authorization"
                    for row in self.ledger.records()
                ):
                    self.ledger.append(
                        provider="cangyuan",
                        event="fallback_selected",
                        reason="explicit_user_authorization",
                    )
                return active_client
            if not failover_enabled:
                active_client = get_primary()
                return active_client
            primary_client = get_primary()
            try:
                primary_client.check_available()
                active_client = primary_client
            except ProviderError as exc:
                # This check is GET-only, so switching here cannot duplicate a paid task.
                active_client = get_fallback()
                active_client.check_available()
                self.ledger.append(provider="cangyuan", event="fallback_selected",
                                   reason="primary_health_check_failed", error=str(exc)[:500])
            return active_client

        def submit_one(clip: dict[str, Any], retry_of: str | None) -> tuple[OmniClient | CangyuanClient, str]:
            if not allow_paid:
                raise PaidRequestBlocked("No known result for this clip; old approval permits queries only")
            if retry_of:
                time.sleep(2)
            if health_check:
                health_check()
            public = ensure_public() if ensure_public else public_by_source
            references = state["reference_sets"][str(clip["index"])]
            urls = [public[str(Path(row["path"]).resolve())] for row in references]
            bindings = [{"position": pos, "role": row["role"], "clip_index": row.get("clip_index"),
                         "shot_index": row.get("shot_index"), "sha256": row["sha256"], "url": urls[pos - 1]}
                        for pos, row in enumerate(references, 1)]
            client = choose_client()
            try:
                task_id = client.submit(clip["omni_prompt"], urls, clip_index=clip["index"],
                    aspect_ratio=state["provider_contract"]["aspect_ratio"], reference_bindings=bindings,
                    max_retries=retry_limit, retry_of=retry_of)
                return client, task_id
            except ProviderError as exc:
                # Only deterministic HTTP rejection is safe to fail over. A timeout,
                # 5xx or invalid response may already have created a paid task.
                if (not failover_enabled or client is not primary
                        or exc.status_code not in {400, 401, 403, 404, 429}):
                    raise
                backup = get_fallback()
                backup.check_available()
                attempted = next((r for r in reversed(self.ledger.records())
                                  if r.get("event") == "attempted" and r.get("provider") == "omni"
                                  and r.get("clip_index") == clip["index"]), None)
                if not attempted:
                    raise
                task_id = backup.submit(clip["omni_prompt"], urls, clip_index=clip["index"],
                    aspect_ratio=state["provider_contract"]["aspect_ratio"], reference_bindings=bindings,
                    max_retries=retry_limit, retry_of=retry_of, fallback_of=attempted.get("attempt_id"))
                self.ledger.append(provider="cangyuan", event="fallback_selected", clip_index=clip["index"],
                                   reason="primary_deterministic_rejection", primary_status=exc.status_code)
                active_client = backup
                return backup, task_id

        def recover_one(clip: dict[str, Any], client: OmniClient | CangyuanClient, task_id: str) -> tuple[int, Path]:
            data = client.poll(task_id)
            target = self.project_dir / "clips" / "raw" / f"clip-{clip['index']:02d}.mp4"
            client.download_result(task_id, data, target)
            return clip["index"], target

        for start in range(0, len(state["clips"]), 2):
            pending = state["clips"][start:start + 2]
            while pending:
                retry_clips: list[dict[str, Any]] = []
                errors: list[Exception] = []
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="3d-sygg-omni") as pool:
                    futures = {}
                    for clip in pending:
                        if clip["index"] in outputs:
                            continue
                        attempts_before = sum(r.get("event") == "attempted" for r in self.ledger.records())
                        try:
                            tasks = [t for t in _merged_omni_tasks(self.ledger.records()).values()
                                     if t["clip_index"] == clip["index"]]
                            attempts = {r["attempt_id"]: r for r in self.ledger.records()
                                        if r.get("event") == "attempted" and r.get("attempt_id")
                                        and r.get("clip_index") == clip["index"]}
                            for previous, following in zip(tasks, tasks[1:]):
                                parent = following.get("retry_of")
                                visited = set()
                                while parent in attempts and parent not in visited:
                                    visited.add(parent)
                                    parent = attempts[parent].get("retry_of")
                                if parent != previous["task_id"]:
                                    raise ValidationError("Multiple unlinked task IDs exist for one clip")
                            task = tasks[-1] if tasks else None
                            if task and task.get("downloaded"):
                                path = Path(str(task.get("path") or ""))
                                if path.is_file() and task.get("sha256") == sha256_file(path):
                                    outputs[clip["index"]] = path
                                    continue
                            parent = self.ledger.retry_parent(clip["index"]) if allow_paid and retry_limit else None
                            if task and task.get("event") == "failed":
                                if not parent:
                                    raise ProviderError(f"Clip {clip['index']} failed; retry allowance exhausted or unavailable. Preserve this project")
                                task_client, task_id = submit_one(clip, parent)
                            elif task:
                                task_client = get_fallback() if task.get("provider") == "cangyuan" else get_primary()
                                task_id = task["task_id"]
                            else:
                                task_client, task_id = submit_one(clip, parent)
                            futures[pool.submit(recover_one, clip, task_client, task_id)] = clip
                        except Exception as exc:
                            if (isinstance(exc, ProviderError) and allow_paid and retry_limit
                                    and sum(r.get("event") == "attempted" for r in self.ledger.records()) > attempts_before
                                    and self.ledger.retry_parent(clip["index"])):
                                retry_clips.append(clip)
                            else:
                                errors.append(exc)
                                break
                    for future in as_completed(futures):
                        clip = futures[future]
                        try:
                            index, path = future.result()
                            outputs[index] = path
                        except Exception as exc:
                            if (isinstance(exc, ProviderError) and allow_paid and retry_limit
                                    and self.ledger.retry_parent(clip["index"])):
                                retry_clips.append(clip)
                            else:
                                errors.append(exc)
                if errors:
                    raise next((e for e in errors if isinstance(e, SubmissionUnknown)), errors[0])
                pending = retry_clips
        return [outputs[int(clip["index"])] for clip in state["clips"]]

    def _replacement_sources(self, state: dict[str, Any]) -> dict[int, dict[str, Any]]:
        result = {}
        for key, row in state.get("clip_replacements", {}).items():
            child = AdOrchestrator(Path(row["project"]))
            revision = child.load()
            child._validate_generation_approval(revision, allowed_states={"segment_ready"})
            link = revision["plan"].get("replacement_for", {})
            index = int(key)
            if (link.get("project") != str(self.project_dir) or link.get("clip_index") != index
                    or link.get("parent_approval") != state["approvals"]["references"]
                    or row.get("approval") != revision["approvals"]["references"]):
                raise ValidationError("Replacement does not belong to this approved full advertisement")
            artifact = revision["artifacts"].get("replacement_video", {})
            path = Path(artifact.get("path", ""))
            if (row.get("sha256") != artifact.get("sha256") or not path.is_file()
                    or sha256_file(path) != row["sha256"]):
                raise ValidationError("Replacement video is missing or changed")
            if not any(t.get("downloaded") and t.get("sha256") == row["sha256"]
                       and t.get("clip_index") == 1 for t in _merged_omni_tasks(child.ledger.records()).values()):
                raise ValidationError("Replacement video has no matching downloaded task")
            result[index] = {**row, "path": str(path), "clip": revision["clips"][0],
                             "reference_approval": str(child.project_dir / "reference-approval.md")}
        return result

    @exclusive_project
    def _accept_replacement(self, child_state: dict[str, Any], child_dir: Path) -> None:
        state = self.load()
        self._validate_generation_approval(state, allowed_states={"failed", "generating", "awaiting_manual_review"})
        link = child_state["plan"]["replacement_for"]
        artifact = child_state["artifacts"]["replacement_video"]
        row = {"project": str(child_dir), "approval": child_state["approvals"]["references"],
               "sha256": artifact["sha256"]}
        key = str(link["clip_index"])
        state.setdefault("clip_replacements", {})[key] = row
        self._replacement_sources(state)
        advanced.invalidate_dependents(state, link["clip_index"], row["sha256"])
        self.save(state)

    def _return_to_parent(self, state: dict[str, Any]) -> dict[str, Any]:
        parent = AdOrchestrator(Path(state["plan"]["replacement_for"]["project"]),
            key_loader=self.key_loader, publisher_cls=self.publisher_cls,
            omni_factory=self.omni_factory, fallback_factory=self.fallback_factory, minimax_factory=self.minimax_factory)
        existing = parent.load()
        if existing["state"] == "delivered":
            row = existing.get("clip_replacements", {}).get(str(state["plan"]["replacement_for"]["clip_index"]), {})
            if row.get("approval") != state["approvals"]["references"] or row.get("project") != str(self.project_dir):
                raise ValidationError("Cannot replace an already delivered advertisement")
            parent._replacement_sources(existing)
            manifest = _read_json(parent.project_dir / "delivery-manifest.json")
            qa = _read_json(parent.project_dir / "qa" / "qa-report.json")
            if sha256_file(Path(manifest["final_video"])) != qa.get("final_sha256"):
                raise ValidationError("Parent delivery changed after QA")
            return manifest
        parent._accept_replacement(state, self.project_dir)
        return parent.resume()

    def _assemble(self, state: dict[str, Any], raw_clips: list[Path], narration: Path | None) -> dict[str, Any]:
        if len(raw_clips) != len(state["clips"]):
            raise ValidationError("Full advertisement requires every planned clip in order")
        if state["plan"]["narration"]["enabled"]:
            cached = self._cached_narration(state)
            if not narration or not cached or sha256_file(narration) != sha256_file(cached):
                raise ValidationError("Approved full-length narration must be present before assembly")
        elif narration:
            raise ValidationError("Narration is not approved for this advertisement")
        replacements = self._replacement_sources(state)
        assembly_rows = []
        tasks = list(_merged_omni_tasks(self.ledger.records()).values())
        effective = copy.deepcopy(state)
        for clip, source in zip(effective["clips"], raw_clips, strict=True):
            digest = sha256_file(source)
            replacement = replacements.get(clip["index"])
            if replacement:
                if digest != replacement["sha256"]:
                    raise ValidationError("Assembly input does not match the approved replacement")
                for key in ("shots", "storyboard_panel_count", "transition_in", "transition_out", "trim_start", "audio"):
                    if key in replacement["clip"]:
                        clip[key] = copy.deepcopy(replacement["clip"][key])
            elif not any(t.get("clip_index") == clip["index"] and t.get("downloaded")
                         and t.get("sha256") == digest for t in tasks):
                raise ValidationError("Assembly input is not the downloaded result for its planned clip")
            if state["schema_version"] >= 9 and state["provider_contract"]["capabilities"]["family"] != "omni":
                advanced.validate_source_video(source, state)
            if state["schema_version"] >= 10 and audio_routing.native(state["plan"]):
                info = probe_media(source)
                end = float(clip.get("trim_start", 0)) + float(clip["keep_duration"])
                if not info["has_audio"] or info["audio_duration"] + 1/30 + .002 < end:
                    raise ValidationError("视频模型未返回覆盖保留片段的声音；保留原任务，不自动调用独立语音模型")
            assembly_rows.append({"clip_index": clip["index"], "path": str(source), "sha256": digest,
                "start": clip["global_start"], "end": clip["global_end"],
                "reference_approval": replacement["reference_approval"] if replacement else str(self.project_dir / "reference-approval.md")})
        if state["plan"].get("replacement_for"):
            state["artifacts"]["replacement_video"] = {"path": str(raw_clips[0]), "sha256": sha256_file(raw_clips[0])}
            state["state"] = "segment_ready"
            self.save(state)
            return self._return_to_parent(state)
        state = effective
        normalized: list[Path] = []
        for clip, source in zip(state["clips"], raw_clips, strict=True):
            target = self.project_dir / "clips" / "normalized" / f"clip-{clip['index']:02d}.mp4"
            normalized.append(
                normalize_clip(
                    source,
                    target,
                    float(clip["keep_duration"]),
                    target_width=int(state["target"]["width"]),
                    target_height=int(state["target"]["height"]),
                    **({"trim_start": float(clip["trim_start"])} if clip.get("trim_start") else {}),
                    **({"align_frames": True} if state["schema_version"] >= 9 else {}),
                )
            )
        stitched = self.project_dir / "final" / "picture-lock.mp4"
        if any(c.get("overlap_frames",0) for c in state["clips"]):
            advanced.stitch_timeline(normalized, state["clips"], stitched)
        else:
            stitch_clips(normalized, stitched)
        final = self.project_dir / "final" / "3d-sygg-commercial.mp4"
        if narration:
            mix_narration(stitched, narration, final, start_time=narration_window(state["plan"], state["target"]["duration"])[0])
        else:
            shutil.copy2(stitched, final)
        review_times = sorted({
            float(clip["global_start"]) + (float(shot["start"]) + float(shot["end"])) / 2
            for clip in state["clips"] for shot in clip["shots"]
        } | {
            max(0, float(clip["global_end"]) - 0.12) for clip in state["clips"]
        } | {
            float(clip["global_start"]) + 0.12 for clip in state["clips"][1:]
        })
        frames = extract_review_frames(final, self.project_dir / "qa" / "frames", timestamps=review_times)
        atomic_write_json(self.project_dir / "qa" / "frame-times.json", [
            {"time": t, "path": str(p)} for t, p in zip(review_times, frames)
        ])
        qa = write_qa_report(
            final, self.project_dir / "qa" / "qa-report.json",
            expected_duration=float(state["target"]["duration"]),
            narration_expected=bool(state["plan"]["narration"]["enabled"]),
            expected_width=int(state["target"]["width"]),
            expected_height=int(state["target"]["height"]),
            **({"expected_frames": state["target"]["frames"]} if state["schema_version"] >= 9 else {}),
        )
        assembly = {"target": state["target"], "clips": assembly_rows,
                    "narration": {"path": str(narration), "sha256": sha256_file(narration),
                        "text": state["plan"]["narration"]["text"],
                        "start_time": narration_window(state["plan"], state["target"]["duration"])[0]} if narration else None}
        if state["schema_version"] >= 10:
            assembly["audio"] = copy.deepcopy(state["provider_contract"]["audio"])
        atomic_write_json(self.project_dir / "assembly.json", assembly)
        state = self.load()
        state["artifacts"] = {"assembly": str(self.project_dir / "assembly.json"),
            "plan": str(self.project_dir / "plan.md"),
            "reference_manifest": str(self.project_dir / "references" / "manifest.json"),
            "request_records": [
                str(path) for path in sorted((self.project_dir / "requests").glob("*.request.json"))
            ],
            "task_ledger": str(self.ledger.path),
            "production_preflight": str(self.project_dir / "preflight-production.json"),
            "publication_evidence": str(self.project_dir / "requests" / "publication"),
            "frame_times": str(self.project_dir / "qa" / "frame-times.json"),
            "narration": str(narration) if narration else None,
            "raw_clips": [str(path) for path in raw_clips],
            "normalized_clips": [str(path) for path in normalized],
            "picture_lock": str(stitched),
            "final_video": str(final),
            "review_frames": [str(path) for path in frames],
            "qa_report": str(self.project_dir / "qa" / "qa-report.json"),
        }
        state["state"] = "awaiting_manual_review" if qa.get("technical_status") == "pass" else "failed"
        self.save(state)
        request_rows = self.ledger.records()
        for project in {r["project"] for r in replacements.values()}:
            request_rows += AdOrchestrator(Path(project)).ledger.records()
        submission_counts = {provider: sum(r.get("provider") == provider and r.get("event") == "attempted" for r in request_rows)
                             for provider in ("omni", "minimax")}
        if any(r.get("provider") == "cangyuan" for r in request_rows):
            submission_counts["cangyuan"] = sum(r.get("provider") == "cangyuan" and r.get("event") == "attempted"
                                                 for r in request_rows)
        manifest = {
            "status": state["state"],
            "submission_counts": submission_counts,
            "submission_count_note": "Original project and active replacements; recorded POST attempts, not provider billing reconciliation",
            "project_id": state["project_id"],
            "final_video": str(final),
            "qa_status": qa["status"],
            "technical_qa_status": qa.get("technical_status"),
            "creative_review_status": qa.get("creative_review_status"),
            "manual_review_required": True,
            "target_duration": state["target"]["duration"],
            "assembly": assembly,
            "paid_counts_approved": state["paid_counts"],
            "artifacts": state["artifacts"],
        }
        atomic_write_json(self.project_dir / "delivery-manifest.json", manifest)
        (self.project_dir / "delivery.md").write_text(_render_delivery(manifest), encoding="utf-8")
        return manifest

    @exclusive_project
    def complete_review(self, review: dict[str, Any]) -> dict[str, Any]:
        """Close the internal visual/listening gate without starting any paid request."""
        state = self.load()
        if state["state"] != "awaiting_manual_review":
            raise ValidationError("Creative review can only be completed from awaiting_manual_review")
        qa_path = self.project_dir / "qa" / "qa-report.json"
        manifest_path = self.project_dir / "delivery-manifest.json"
        qa = _read_json(qa_path)
        manifest = _read_json(manifest_path)
        if qa.get("technical_status") != "pass":
            raise ValidationError("Technical QA must pass before creative review")
        if state["schema_version"] >= 8:
            self._validate_generation_approval(state, allowed_states={"awaiting_manual_review"})
            if state["plan"].get("replacement_for"):
                raise ValidationError("A replacement segment cannot be delivered as the full advertisement")
            assembly = _read_json(self.project_dir / "assembly.json")
            if (assembly.get("target") != state["target"] or
                    [r["clip_index"] for r in assembly.get("clips", [])] != [c["index"] for c in state["clips"]]
                    or bool(assembly.get("narration")) != state["plan"]["narration"]["enabled"]):
                raise ValidationError("Full advertisement assembly does not match approved scope")
            if state["schema_version"] >= 10 and assembly.get("audio") != state["provider_contract"]["audio"]:
                raise ValidationError("Assembly audio route changed after approval")
            for row in assembly["clips"] + ([assembly["narration"]] if assembly.get("narration") else []):
                path = Path(row["path"])
                if not path.is_file() or sha256_file(path) != row["sha256"]:
                    raise ValidationError("Assembly source changed after QA")
            final = Path(manifest["final_video"])
            if not final.is_file() or sha256_file(final) != qa.get("final_sha256"):
                raise ValidationError("Final MP4 changed after technical QA; review cannot approve a different file")
        checks = review.get("checks")
        if not isinstance(checks, dict):
            raise ValidationError("Creative review requires a checks object")
        missing = [key for key in CREATIVE_REVIEW_CHECKS if not isinstance(checks.get(key), bool)]
        if missing:
            raise ValidationError("Creative review requires boolean checks: " + ", ".join(missing))
        reviewer = str(review.get("reviewer") or "").strip()
        if not reviewer:
            raise ValidationError("Creative review requires reviewer")
        passed = all(checks[key] for key in CREATIVE_REVIEW_CHECKS)
        creative_review = {
            "status": "pass" if passed else "failed",
            "reviewer": reviewer,
            "checks": {key: checks[key] for key in CREATIVE_REVIEW_CHECKS},
            "notes": str(review.get("notes") or "").strip(),
        }
        qa["creative_review_status"] = creative_review["status"]
        qa["creative_review"] = creative_review
        qa["status"] = "pass" if passed else "awaiting_manual_review"
        atomic_write_json(qa_path, qa)
        state["state"] = "delivered" if passed else "awaiting_manual_review"
        self.save(state)
        manifest["status"] = state["state"]
        manifest["qa_status"] = qa["status"]
        manifest["creative_review_status"] = creative_review["status"]
        manifest["manual_review_required"] = not passed
        atomic_write_json(manifest_path, manifest)
        (self.project_dir / "delivery.md").write_text(_render_delivery(manifest), encoding="utf-8")
        return manifest

    def _validate_plan_approval(self, state: dict[str, Any]) -> None:
        expected = state.get("approvals", {}).get("plan")
        actual = canonical_hash(_plan_contract(state))
        if not expected or expected != actual:
            raise ValidationError("Plan changed or was not approved; confirmation is invalid")

    def _validate_reference_files(self, state: dict[str, Any]) -> None:
        if not state.get("references"):
            raise ValidationError("No references are registered")
        if state["schema_version"] >= 9 and any(c["execution_strategy"] not in {"full_storyboard", "per_shot"} for c in state["clips"]):
            advanced.verify_bindings(self, state)
        elif state["schema_version"] >= 8:
            for clip in state["clips"]:
                rows = state["reference_sets"].get(str(clip["index"]), [])
                roles = execution_roles(state["plan"], clip)
                actual_roles = ["storyboard" if r["role"] == "storyboard_reference" else r["role"] for r in rows]
                if actual_roles != roles:
                    raise ValidationError("Execution bindings differ from the approved plan")
                for row, role in zip(rows, roles):
                    match = next((r for r in state["references"] if r["role"] == role
                                  and (role != "storyboard" or r["clip_index"] == clip["index"])), None)
                    if not match or any(row.get(k) != match.get(k) for k in ("path", "sha256", "origin")):
                        raise ValidationError("Execution reference is not a registered approved asset")
        all_rows = list(state["references"]) + [r for rows in state.get("reference_sets", {}).values() for r in rows]
        for row in _unique_approved_files(all_rows):
            path = Path(row["path"])
            if not path.is_file() or sha256_file(path) != row["sha256"]:
                raise ValidationError(f"Approved reference is missing or changed: {path}")

    def _validate_source_files(self, state: dict[str, Any]) -> None:
        for row in state.get("source_assets", []):
            path = Path(str(row.get("path") or ""))
            if not path.is_file() or sha256_file(path) != row.get("sha256"):
                raise ValidationError(f"Frozen source image is missing or changed: {path}")
            validate_reference_image(path)

    def _validate_generation_approval(
        self,
        state: dict[str, Any],
        *,
        allowed_states: set[str] | None = None,
    ) -> None:
        allowed = allowed_states or {"approved_for_generation"}
        if state["state"] not in allowed:
            raise ValidationError("Final reference approval is required before preflight or generation")
        self._validate_plan_approval(state)
        self._validate_reference_files(state)
        expected = state.get("approvals", {}).get("references")
        if not expected or expected != canonical_hash(_reference_contract(state)):
            raise ValidationError("References or prompts changed after approval")
        paid_hash = state.get("approvals", {}).get("paid_counts")
        if paid_hash != canonical_hash(state["paid_counts"]):
            raise ValidationError("Approved paid request count changed")

    def load(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            raise ValidationError(f"Project state is missing: {self.state_path}")
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Project state cannot be read: {self.state_path}") from exc
        if not isinstance(data, dict) or data.get("state") not in NORMAL_STATES | ERROR_STATES:
            raise ValidationError("Project state file is invalid")
        if data.get("schema_version") not in {7, 8, 9, PROJECT_SCHEMA_VERSION}:
            raise ValidationError(
                f"Unsupported project schema; create a new project with schema version {PROJECT_SCHEMA_VERSION}"
            )
        validate_target_contract(data)
        return data

    def save(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.state_path, state)


def _validate_analysis(analysis: dict[str, Any]) -> None:
    if not isinstance(analysis, dict):
        raise ValidationError("analysis must be an object")
    if not str(analysis.get("product_type") or "").strip():
        raise ValidationError("analysis.product_type is required")
    source_images = analysis.get("source_images")
    fictional = analysis.get("source_mode") == "authorized_fictional_concept"
    if fictional and (analysis.get("fictional") is not True or not str(analysis.get("user_authorization") or "").strip()):
        raise ValidationError("Fictional concept mode requires explicit recorded user authorization")
    minimum = 0 if fictional else 1
    if not isinstance(source_images, list) or not minimum <= len(source_images) <= 6:
        raise ValidationError("analysis.source_images must contain 1 to 6 local product-image paths")
    if any(not str(path or "").strip() for path in source_images):
        raise ValidationError("analysis.source_images must not contain empty paths")
    for key in ("visible_features", "materials", "colors", "uncertainties"):
        if not isinstance(analysis.get(key), list):
            raise ValidationError(f"analysis.{key} must be a list")
    if not analysis["visible_features"]:
        raise ValidationError("analysis.visible_features must not be empty")
    for color in analysis["colors"]:
        if not HEX_COLOR.fullmatch(str(color)):
            raise ValidationError(f"Observed color must use #RRGGBB: {color}")


def narration_window(plan: dict[str, Any], duration: float) -> tuple[float, float]:
    narration = plan["narration"]
    try:
        start, end = float(narration.get("start_time", 0)), float(narration.get("end_time", duration))
    except (TypeError, ValueError) as exc:
        raise ValidationError("Narration timing must use seconds") from exc
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration:
        raise ValidationError("Narration window must fit inside the full advertisement")
    return start, end - start


def _validate_plan(plan: dict[str, Any], keep_durations: list[float]) -> None:
    if not isinstance(plan, dict):
        raise ValidationError("plan must be an object")
    recovery = plan.get("recovery_policy", {"max_video_retries_per_clip": 0})
    if (not isinstance(recovery, dict) or type(recovery.get("max_video_retries_per_clip")) is not int
            or not 0 <= recovery["max_video_retries_per_clip"] <= 3):
        raise ValidationError("recovery_policy.max_video_retries_per_clip must be an integer from 0 to 3")
    clip_count = len(keep_durations)
    for key in ("big_idea", "style_rationale", "story_arc", "audiovisual_tone", "global_anchor"):
        if not str(plan.get(key) or "").strip():
            raise ValidationError(f"plan.{key} is required")
    if not isinstance(plan.get("aspect_ratio"), str) or plan["aspect_ratio"] not in SUPPORTED_ASPECT_RATIOS:
        raise ValidationError("plan.aspect_ratio must be 9:16 or 16:9")
    if not str(plan.get("aspect_ratio_reason") or "").strip():
        raise ValidationError("plan.aspect_ratio_reason is required")
    if plan.get("style") not in STYLE_NAMES:
        raise ValidationError(f"plan.style must be one of: {', '.join(STYLE_NAMES)}")
    palette = plan.get("palette")
    if not isinstance(palette, dict):
        raise ValidationError("plan.palette must be an object")
    for key in ("primary", "secondary", "rim"):
        if not HEX_COLOR.fullmatch(str(palette.get(key) or "")):
            raise ValidationError(f"plan.palette.{key} must use #RRGGBB")
    ad_copy = plan.get("ad_copy")
    if not isinstance(ad_copy, list):
        raise ValidationError("plan.ad_copy must be a list of approved advertising text")
    for item in ad_copy:
        if not isinstance(item, str) or not item.strip() or "\n" in item:
            raise ValidationError("Each plan.ad_copy item must be a nonempty single-line string")
    talent = plan.get("talent_strategy")
    if not isinstance(talent, dict) or talent.get("mode") not in TALENT_MODE_NAMES:
        raise ValidationError(f"plan.talent_strategy.mode must be one of: {', '.join(TALENT_MODE_NAMES)}")
    if not str(talent.get("reason") or "").strip():
        raise ValidationError("plan.talent_strategy.reason is required")
    framing = talent.get("framing")
    actions = talent.get("interaction_actions")
    if not isinstance(actions, list):
        raise ValidationError("plan.talent_strategy.interaction_actions must be a list")
    if talent["mode"] == "human_interaction":
        if framing not in HUMAN_FRAMINGS:
            raise ValidationError(f"Human interaction framing must be one of: {', '.join(sorted(HUMAN_FRAMINGS))}")
        if talent.get("adult_only") is not True:
            raise ValidationError("Human interaction mode requires adult_only=true")
        for key in ("persona", "wardrobe", "grooming", "identity_anchor"):
            if not str(talent.get(key) or "").strip():
                raise ValidationError(f"plan.talent_strategy.{key} is required in human interaction mode")
        if not actions or any(not str(item or "").strip() for item in actions):
            raise ValidationError("Human interaction mode requires at least one interaction action")
    else:
        if framing != "none" or actions:
            raise ValidationError("Pure product mode requires framing=none and no interaction actions")
    narration = plan.get("narration")
    if not isinstance(narration, dict) or not isinstance(narration.get("enabled"), bool):
        raise ValidationError("plan.narration.enabled must be a boolean")
    narration_window(plan, sum(keep_durations))
    if not narration["enabled"] and (not str(narration.get("reason") or "").strip() or str(narration.get("text") or "").strip()):
        raise ValidationError("No narration requires an explicit creative/user reason and empty speech text")
    if narration["enabled"]:
        for key in ("text", "voice_id", "voice_name", "reason"):
            if not str(narration.get(key) or "").strip():
                raise ValidationError(f"plan.narration.{key} is required when narration is enabled")
        try:
            speed = float(narration.get("speed", 1.0))
        except (TypeError, ValueError) as exc:
            raise ValidationError("plan.narration.speed must be a number") from exc
        if not 0.5 <= speed <= 2.0:
            raise ValidationError("plan.narration.speed must be between 0.5 and 2.0")
    clips = plan.get("clips")
    if not isinstance(clips, list) or len(clips) != clip_count:
        raise ValidationError(f"plan.clips must contain exactly {clip_count} items")
    expected = list(range(1, clip_count + 1))
    try:
        actual = sorted(int(row.get("index", 0)) for row in clips if isinstance(row, dict))
    except (TypeError, ValueError) as exc:
        raise ValidationError("plan.clips indexes must be whole numbers") from exc
    if actual != expected:
        raise ValidationError(f"plan.clips indexes must be {expected}")
    seen_narrative_roles: set[str] = set()
    seen_clip_blueprints: set[tuple[tuple[str, ...], ...]] = set()
    for row in clips:
        for key in (
            "narrative_role",
            "storyboard_reason",
            "visual_progression",
            "camera",
            "transition_in",
            "transition_out",
            "sfx",
        ):
            if not str(row.get(key) or "").strip():
                raise ValidationError(f"Each plan clip requires {key}")
        narrative_role = str(row["narrative_role"]).strip()
        if narrative_role in seen_narrative_roles:
            raise ValidationError("Each clip narrative_role must be distinct across the complete advertisement")
        seen_narrative_roles.add(narrative_role)
        shots = row.get("shots")
        if not isinstance(shots, list) or not 1 <= len(shots) <= 4:
            raise ValidationError("Each plan clip must contain 1 to 4 shots under this workflow")
        execution_roles(plan, row)
        for shot in shots:
            if not isinstance(shot, dict):
                raise ValidationError("Each shot must be an object")
            for key in ("purpose", "visual", "action", "product_detail", "camera", "transition"):
                if not str(shot.get(key) or "").strip():
                    raise ValidationError(f"Each shot requires {key}")
            graphics = shot.get("graphics", [])
            if not isinstance(graphics, list):
                raise ValidationError("shot.graphics must be a list")
            for graphic in graphics:
                if not isinstance(graphic, dict) or graphic.get("kind") not in ("text", "icon", "effect"):
                    raise ValidationError("Each graphic kind must be text, icon or effect")
                for field in ("content", "direction"):
                    if not isinstance(graphic.get(field), str) or not graphic[field].strip():
                        raise ValidationError(f"Each graphic requires {field}")
                if graphic["kind"] == "text" and graphic["content"] not in ad_copy:
                    raise ValidationError("Graphic text must match an approved ad_copy item exactly")
            try:
                weight = float(shot.get("duration_weight", 1.0))
            except (TypeError, ValueError) as exc:
                raise ValidationError("Shot duration_weight must be a number") from exc
            if not math.isfinite(weight) or weight <= 0:
                raise ValidationError("Shot duration_weight must be greater than zero")
        blueprint = tuple(
            tuple(str(shot[key]).strip() for key in ("purpose", "visual", "action", "product_detail", "camera"))
            + (canonical_hash(shot.get("graphics", [])),)
            for shot in shots
        )
        if blueprint in seen_clip_blueprints:
            raise ValidationError(
                "Two clips repeat the same shot blueprint; design distinct visual progression for each video segment"
            )
        seen_clip_blueprints.add(blueprint)
        talent_action = str(row.get("talent_action") or "").strip()
        if talent["mode"] == "pure_product" and talent_action:
            raise ValidationError("Pure product clips must not contain talent_action")

    if talent["mode"] == "human_interaction" and not any(clip_has_talent(plan, row) for row in clips):
        raise ValidationError("Human mode requires an actual planned appearance; otherwise use pure_product")


def _plan_contract(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis": state["analysis"],
        "source_assets": state["source_assets"],
        "plan": state["plan"],
        "target": state["target"],
        "provider_contract": state["provider_contract"],
        "global_anchor": state["global_anchor"],
        "product_master_prompt": state["product_master_prompt"],
        "talent_reference_prompt": state.get("talent_reference_prompt"),
        "reference_asset_plan": state["reference_asset_plan"],
        "clips": state["clips"],
        "paid_counts": state["paid_counts"],
    }


def _reference_contract(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_hash": canonical_hash(_plan_contract(state)),
        "references": state["references"],
        "reference_sets": state["reference_sets"],
        "paid_counts": state["paid_counts"],
    }


def _unique_approved_files(
    reference_sets: dict[str, list[dict[str, Any]]] | list[dict[str, Any]],
) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    rows = (
        [row for group in reference_sets.values() for row in group]
        if isinstance(reference_sets, dict)
        else reference_sets
    )
    for row in rows:
        path = str(Path(row["path"]).resolve())
        unique[path] = {"path": path, "sha256": str(row["sha256"])}
    return list(unique.values())


def _merged_omni_tasks(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for row in records:
        if row.get("provider") not in {"omni", "cangyuan"} or not row.get("task_id"):
            continue
        task_id = str(row["task_id"])
        task = tasks.setdefault(task_id, {"task_id": task_id})
        task["provider"] = row.get("provider")
        if row.get("clip_index") is not None:
            task["clip_index"] = int(row["clip_index"])
        if row.get("event") == "submitted":
            task["retry_of"] = row.get("retry_of")
        if row.get("event") in {"failed", "completed", "downloaded"}:
            task["event"] = row["event"]
            task["status"] = row.get("status")
        if row.get("event") == "downloaded":
            task["downloaded"] = True
            task["path"] = row.get("path")
            task["sha256"] = row.get("sha256")
    return {task_id: task for task_id, task in tasks.items() if task.get("clip_index") is not None}


def _md_cell(value: Any) -> str:
    """Keep generated director-board tables readable when prose contains Markdown separators."""
    return str(value).replace("\n", " ").replace("|", "\\|").strip()


def _shot_time_range(clip: dict[str, Any], shot: dict[str, Any], *, global_time: bool) -> str:
    offset = float(clip["global_start"]) if global_time else 0.0
    return f"{float(shot['start']) + offset:.2f}–{float(shot['end']) + offset:.2f}s"


def _render_plan(state: dict[str, Any]) -> str:
    # Include the same image model/cost contract in both legacy and extended reports.
    image_header = image_api.render_contract(state)
    if state.get("schema_version", 0) >= 9 and advanced.needs_extended_report(state):
        return image_header + advanced.render_plan(state)
    narration = state["plan"]["narration"]
    talent = state["plan"]["talent_strategy"]
    lines = [
        f"# 3D sygg 广告方案：{state['project_id']}",
        "",
        "## 导演总览",
        "",
        f"- 商品：{state['analysis']['product_type']}",
        f"- 时长：{state['target']['duration']} 秒",
        f"- Big Idea：{state['plan']['big_idea']}",
        f"- 主风格：{STYLE_NAMES[state['plan']['style']]}",
        f"- 为什么适合该商品：{state['plan']['style_rationale']}",
        f"- 全片故事弧：{state['plan']['story_arc']}",
        f"- 声音方向：{state['plan']['audiovisual_tone']}",
        f"- 规格：{state['target']['aspect_ratio']}，{state['target']['width']}×{state['target']['height']}，{state['target']['duration']} 秒",
        f"- 画幅选择理由：{state['target']['aspect_ratio_reason']}",
        f"- 出镜策略：{TALENT_MODE_NAMES[talent['mode']]}（{talent['reason']}）",
        f"- 付费次数：Omni {state['paid_counts']['omni']} 次；MiniMax {state['paid_counts']['minimax']} 次",
        f"- 模型：`{state['provider_contract']['model']}`；默认 wxart 使用其当前公开别名 `omni-flash`，降级 Cangyuan 使用同名模型 ID",
        "- 降级通道：默认 wxart 不可用且未创建任务时，才切换 Cangyuan；提交不明不盲目重付费",
        f"- 自动恢复：每个明确失败的视频段最多重试 {state['plan'].get('recovery_policy', {}).get('max_video_retries_per_clip', 0)} 次；含重试最多 {state['paid_counts']['omni'] * (1 + state['plan'].get('recovery_policy', {}).get('max_video_retries_per_clip', 0))} 次视频调用。重试可能额外收费；成功段复用，不重做方案或参考图。",
        f"- 声音制作：{audio_routing.summary(state['plan'])}",
        f"- 最少生图次数：{state['reference_asset_plan']['minimum_imagegen_calls']} 次；本次生成："
        + "、".join({"product_master": "商品母版", "talent": "成年人物设定"}.get(role, "分镜 " + role.split(":")[-1])
                   for role in state['reference_asset_plan']['generation_order']),
        "- 原始商品图：仅用于生成商品母版和核对事实，不进入 Omni，不通过临时公网发布",
    ]
    if talent["mode"] == "human_interaction":
        lines += [
            f"- 人物景别：{talent['framing']}",
            f"- 人物定位：{talent['persona']}",
            f"- 服装妆造：{talent['wardrobe']}；{talent['grooming']}",
            f"- 人物一致性：{talent['identity_anchor']}",
        ]
    if narration["enabled"]:
        lines += [f"- 音色：{narration['voice_name']}（{narration['voice_id']}）", f"- 旁白文案：{narration['text']}"]
    lines += [
        "",
        "## 全片分段地图",
        "",
        "| 视频段 | 全片时间 | 上游请求 | 执行分镜 | 本段叙事任务 | 入场衔接 | 出场衔接 |",
        "|---|---:|---:|---|---|---|---|",
    ]
    for clip in state["clips"]:
        lines.append(
            f"| {clip['index']} | {clip['global_start']:.2f}–{clip['global_end']:.2f}s | "
            f"10 秒，保留 {clip['keep_duration']} 秒 | "
            f"{STORYBOARD_LAYOUTS[clip['storyboard_panel_count']][1]} | "
            f"{_md_cell(clip['narrative_role'])} | {_md_cell(clip['transition_in'])} | "
            f"{_md_cell(clip['transition_out'])} |"
        )
    for clip in state["clips"]:
        lines += [
            "",
            f"## 视频段 {clip['index']}：{clip['global_start']:.2f}–{clip['global_end']:.2f}s",
            "",
            f"- 参考图结构：{STORYBOARD_LAYOUTS[clip['storyboard_panel_count']][1]}；{clip['storyboard_reason']}",
            f"- 执行图片顺序：{' → '.join(clip.get('execution_reference_roles', ['storyboard']))}",
            f"- 素材选择理由：{clip.get('execution_reference_reason', '')}",
            f"- 画面演进：{clip['visual_progression']}",
            f"- 运镜总览：{clip['camera']}",
            f"- 人物动作：{clip['talent_action'] or '无人物'}",
            f"- 音效：{clip['sfx']}",
            "",
            "| 格位 | 全片时间 | 片段内时间 | 叙事任务 | 画面构图 | 可见动作 | 商品细节 | 运镜 | 转场 |",
            "|---:|---:|---:|---|---|---|---|---|---|",
        ]
        for shot in clip["shots"]:
            lines.append(
                f"| {shot['index']} | {_shot_time_range(clip, shot, global_time=True)} | "
                f"{_shot_time_range(clip, shot, global_time=False)} | {_md_cell(shot['purpose'])} | "
                f"{_md_cell(shot['visual'])} | {_md_cell(shot['action'])} | "
                f"{_md_cell(shot['product_detail'])} | {_md_cell(shot['camera'])} | "
                f"{_md_cell(shot['transition'])} |"
            )
        for shot in clip["shots"]:
            if shot.get("graphics"):
                lines.append(f"- 镜头 {shot['index']} 图文与特效：{graphics_direction(shot)}")
    lines += ["", "等待确认：**确认方案，生成参考图**", ""]
    link = state["plan"].get("replacement_for")
    if link:
        lines[2:2] = [f"**本项目只替换原广告第 {link['clip_index']} 段；最终交付仍为 {link['target']['duration']} 秒完整广告。**",
            f"原项目：{link['project']}；生成后自动返回原项目拼接；旁白继承原片设置，不单独交付本片段。",
            f"本次新增生图：{state['reference_asset_plan']['generation_order']}；复用身份图：{[r['role'] for r in state['reference_asset_plan'].get('reusable_assets', [])]}。", ""]
    voice = link["narration"] if link else state["plan"]["narration"]
    full_duration = link["target"]["duration"] if link else state["target"]["duration"]
    lines += ["", "全片中文旁白：" + (audio_routing.summary(state["plan"]) if state.get("schema_version", 0) >= 10 else (voice['text'] if voice['enabled'] else "不启用；理由：" + str(voice.get('reason', '旧计划未说明')))),
              f"旁白窗口：{narration_window({'narration': voice}, full_duration)}（起始秒，最多持续秒）；与分段无关。" if voice['enabled'] else ""]
    if state.get("schema_version",0)>=9:
        lines += ["", advanced.frozen_summary(state)]
    return image_header + "\n".join(lines)


def _render_reference_approval(state: dict[str, Any]) -> str:
    if state.get("schema_version", 0) >= 9 and advanced.needs_extended_report(state):
        return advanced.render_references(state)
    narration = state["plan"]["narration"]
    narration_summary = audio_routing.summary(state["plan"]) if state.get("schema_version", 0) >= 10 else (
        f"启用，{narration['voice_name']}（{narration['voice_id']}）"
        if narration["enabled"]
        else "不启用"
    )
    lines = [
        f"# 3D sygg 参考图确认：{state['project_id']}",
        "",
        "## 导演总览",
        "",
        f"- 商品：{state['analysis']['product_type']}",
        f"- 总时长与画幅：{state['target']['duration']} 秒，{state['target']['aspect_ratio']}，{state['target']['width']}×{state['target']['height']}",
        f"- 主风格：{STYLE_NAMES[state['plan']['style']]}",
        f"- 为什么适合该商品：{state['plan']['style_rationale']}",
        f"- 全片故事弧：{state['plan']['story_arc']}",
        f"- 声音方向：{state['plan']['audiovisual_tone']}",
        f"- 广告画面文字：{' / '.join(str(item) for item in state['plan']['ad_copy'])}",
        f"- 旁白：{narration_summary}",
        "",
        "## 商品与人物资产（是否上传以下方实际清单为准）",
        "",
    ]
    role_names = {
        "product_master": "商品母版",
        "talent": "成年人物设定",
        "style": "风格/材质图",
    }
    for row in state["references"]:
        if row["role"] == "storyboard":
            continue
        label = role_names.get(row["role"], row["role"])
        clip_note = f"，片段 {row['clip_index']}" if row.get("clip_index") else ""
        lines += [
            f"### {label}{clip_note}",
            "",
            f"![{label}](<{row['path']}>)",
            "",
            f"- 尺寸：{row['width']}×{row['height']}；SHA-256：`{row['sha256']}`",
            "",
        ]
    lines += [
        "## 实际上传 Omni 的执行参考图",
        "",
        "Image1 为完整分镜；其余图片仅按下列清单约束商品或成年人物身份。文件原样发布，不拆格、不裁切、不重新编码；用户原图绝不公开。",
        "",
    ]
    for clip in state["clips"]:
        references = state["reference_sets"][str(clip["index"])]
        strategy = clip.get("omni_reference_strategy") or "unknown"
        lines += [
            f"### 视频段 {clip['index']}：全片 {clip['global_start']:.2f}–{clip['global_end']:.2f}s（{strategy}）",
            "",
            f"- 本段叙事任务：{clip['narrative_role']}",
            f"- 分镜选择理由：{clip['storyboard_reason']}",
            f"- 执行图片顺序：{' → '.join(clip.get('execution_reference_roles', ['storyboard']))}",
            f"- 素材选择理由：{clip.get('execution_reference_reason', '')}",
            f"- 入场衔接：{clip['transition_in']}",
            f"- 出场衔接：{clip['transition_out']}",
            f"- 本段音效：{clip['sfx']}",
            "",
        ]
        for shot in clip["shots"]:
            if shot.get("graphics"):
                lines.append(f"- 镜头 {shot['index']} 图文与特效：{graphics_direction(shot)}")
        for position, row in enumerate(references, start=1):
            reading_order_cn = {
                "single full-frame composition": "单一完整画面",
                "left to right in one row": "单行从左到右",
                "top-left, top-right, bottom-left, bottom-right": "左上、右上、左下、右下",
            }.get(clip["storyboard_reading_order"], clip["storyboard_reading_order"])
            shot_note = (
                f"，{clip['storyboard_panel_count']} 个故事节点，读取顺序："
                f"{reading_order_cn}"
            ) if row["role"] == "storyboard_reference" else "，仅约束身份，不控制构图和运镜"
            lines += [
                f"#### Image{position}：{row['role']}{shot_note}",
                "",
                f"![Image{position}](<{row['path']}>)",
                "",
                f"- 尺寸：{row['width']}×{row['height']}；SHA-256：`{row['sha256']}`",
                "",
            ]
        lines += [
            "#### 图片外导演标注（不会烧进参考图）",
            "",
            "| 格位 | 全片时间 | 片段内时间 | 叙事任务 | 可见动作 | 商品细节 | 运镜 | 转场 |",
            "|---:|---:|---:|---|---|---|---|---|",
        ]
        for shot in clip["shots"]:
            lines.append(
                f"| {shot['index']} | {_shot_time_range(clip, shot, global_time=True)} | "
                f"{_shot_time_range(clip, shot, global_time=False)} | {_md_cell(shot['purpose'])} | "
                f"{_md_cell(shot['action'])} | {_md_cell(shot['product_detail'])} | "
                f"{_md_cell(shot['camera'])} | {_md_cell(shot['transition'])} |"
            )
        lines.append("")
    lines += [
        "## 生成规格与付费确认",
        "",
        f"- 成片：{state['target']['aspect_ratio']}，{state['target']['width']}×{state['target']['height']}，{state['target']['duration']} 秒",
        f"- 模型：`{state['provider_contract']['model']}`；wxart 当前目录使用服务商别名 `omni-flash`，`mode=ref`、每次 10 秒、720p、{state['target']['aspect_ratio']}、watermark=false",
        "- 降级：Cangyuan `omni-fast-no-water`、`images` 图生视频字段、每次 10 秒；仅处理默认通道的明确未创建任务拒绝或只读健康检查失败",
        f"- 预计付费次数：Omni {state['paid_counts']['omni']} 次；MiniMax {state['paid_counts']['minimax']} 次",
        f"- 自动恢复上限：每失败段 {state['plan'].get('recovery_policy', {}).get('max_video_retries_per_clip', 0)} 次重试；视频调用总上限 {state['paid_counts']['omni'] * (1 + state['plan'].get('recovery_policy', {}).get('max_video_retries_per_clip', 0))} 次，可能产生额外费用；不包含更换方案或图片。",
        "",
        "等待确认：**确认参考图并生成视频**",
        "",
    ]
    link = state["plan"].get("replacement_for")
    if link:
        lines[2:2] = [f"**本项目只替换原广告第 {link['clip_index']} 段；最终交付仍为 {link['target']['duration']} 秒完整广告。**",
            f"原项目：{link['project']}；生成后自动返回原项目拼接；旁白继承原片设置，不单独交付本片段。",
            f"本次新增生图：{state['reference_asset_plan']['generation_order']}；复用身份图：{[r['role'] for r in state['reference_asset_plan'].get('reusable_assets', [])]}。", ""]
    voice = link["narration"] if link else state["plan"]["narration"]
    full_duration = link["target"]["duration"] if link else state["target"]["duration"]
    lines += ["", "全片中文旁白：" + (audio_routing.summary(state["plan"]) if state.get("schema_version", 0) >= 10 else (voice['text'] if voice['enabled'] else "不启用；理由：" + str(voice.get('reason', '旧计划未说明')))),
              f"旁白窗口：{narration_window({'narration': voice}, full_duration)}（起始秒，最多持续秒）；与分段无关。" if voice['enabled'] else ""]
    if state.get("schema_version",0)>=9:
        lines += ["", advanced.frozen_summary(state)]
    return "\n".join(lines)


def _render_delivery(manifest: dict[str, Any]) -> str:
    creative = manifest.get("creative_review_status") or "pending"
    creative_note = (
        "创意复核已通过，可交付。"
        if creative == "pass"
        else "尚未通过逐镜观看与试听，当前文件不得标记为最终交付。"
    )
    return "\n".join(
        [
            f"# 3D sygg 交付：{manifest['project_id']}",
            "",
            f"- 状态：{manifest['status']}",
            f"- 最终视频：{manifest['final_video']}",
            f"- 技术 QA：{manifest['qa_status']}",
            f"- 创意复核：{creative_note}",
            "",
        ]
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Could not read JSON file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"JSON root must be an object: {path}")
    return data


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="3D sygg commercial-ad orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="validate and create a project plan")
    prepare.add_argument("--project-id", required=True)
    prepare.add_argument("--analysis", type=Path, required=True)
    prepare.add_argument("--plan", type=Path, required=True)
    prepare.add_argument("--target-duration", type=float, help="user-requested final seconds; otherwise director plan supplies duration and reason")
    prepare.add_argument('--model', help='omni / sd11-seedance-2.0 / sd11-seedance-2.5 / mm2-minimax-h3')
    prepare.add_argument('--strategy', choices=['auto', *caps.STRATEGIES])
    prepare.add_argument('--output-resolution', choices=['720p','1080p'])
    prepare.add_argument('--validation-run', action='store_true', help='explicitly budgeted unqualified-route benchmark')
    prepare.add_argument("--parent-project", type=Path, help="full advertisement receiving this replacement")
    prepare.add_argument("--replace-clip", type=int, help="original clip index, paired with parent-project")
    prepare.add_argument(
        "--video-provider",
        choices=["auto", "wxart", "cangyuan"],
        default="auto",
        help="freeze automatic routing or an explicitly authorized video provider into the approval",
    )
    prepare.add_argument("--output-root", type=Path, default=Path("outputs"))

    for command in (
        "approve-plan",
        "approve-references",
        "preflight",
        "produce",
        "resume",
        "status",
        "complete-review",
    ):
        item = sub.add_parser(command)
        item.add_argument("--project", type=Path, required=True)
        if command == "preflight":
            item.add_argument("--verify-tunnel", action="store_true", help="optional publication diagnostic")
        if command == "complete-review":
            item.add_argument("--review", type=Path, required=True)

    register = sub.add_parser("register-references")
    register.add_argument("--project", type=Path, required=True)
    register.add_argument("--manifest", type=Path, required=True)

    sub.add_parser('capabilities', help='show documented and verified route capabilities')
    config = sub.add_parser('configure', help='choose defaults for future projects; no credentials')
    config.add_argument('--model', required=True)
    config.add_argument('--provider', default='auto', choices=['auto','wxart','cangyuan'])
    config.add_argument('--output-resolution', default='720p', choices=['720p','1080p'])
    config.add_argument('--strategy', default='auto', choices=['auto', *caps.STRATEGIES])
    check = sub.add_parser('check-channel', help='GET-only model visibility; never a generation test')
    check.add_argument('--model', required=True)
    check.add_argument('--provider', default='auto', choices=['auto','wxart','cangyuan'])
    continuity = sub.add_parser('review-continuity', help='record inspection of an extracted continuity frame')
    continuity.add_argument('--project', type=Path, required=True)
    continuity.add_argument('--review', type=Path, required=True)
    voices = sub.add_parser("list-voices", help="read system voices and suggest one")
    voices.add_argument("--category", choices=["tech", "luxury", "lifestyle", "youth"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        orchestrator = AdOrchestrator.create(args.project_id, args.output_root)
        result = orchestrator.prepare(
            _read_json(args.analysis), _read_json(args.plan), target_duration=args.target_duration,
            parent_project=args.parent_project, replace_clip=args.replace_clip,
            video_provider=args.video_provider, model=args.model, strategy=args.strategy,
            output_resolution=args.output_resolution, validation_run=args.validation_run,
        )
    elif args.command == "capabilities":
        result = {name: caps.route(name) for name in caps.ROUTES}
    elif args.command == "configure":
        result = caps.configure(args.model, args.provider, args.output_resolution, args.strategy)
    elif args.command == "check-channel":
        result = advanced.check_channel(caps.route(args.model, args.provider))
    elif args.command == "review-continuity":
        result = advanced.review_continuity(AdOrchestrator(args.project), _read_json(args.review))
    elif args.command == "list-voices":
        ledger = TaskLedger(Path(os.devnull))
        key = Keychain.load(MINIMAX_KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        voices = MiniMaxClient(key, ledger).list_system_voices()
        result = {"system_voice": voices}
        if args.category:
            result["suggested"] = choose_system_voice(args.category, voices)
    else:
        orchestrator = AdOrchestrator(args.project)
        if args.command == "approve-plan":
            result = orchestrator.approve_plan()
        elif args.command == "register-references":
            result = orchestrator.register_references(_read_json(args.manifest))
        elif args.command == "approve-references":
            result = orchestrator.approve_references()
        elif args.command == "preflight":
            result = orchestrator.preflight(verify_tunnel=args.verify_tunnel)
        elif args.command == "produce":
            result = orchestrator.produce()
        elif args.command == "resume":
            result = orchestrator.resume()
        elif args.command == "complete-review":
            result = orchestrator.complete_review(_read_json(args.review))
        elif args.command == "status":
            result = orchestrator.load()
        else:  # pragma: no cover
            raise ValidationError(f"Unsupported command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValidationError, ProviderError, SubmissionUnknown, PaidRequestBlocked) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
