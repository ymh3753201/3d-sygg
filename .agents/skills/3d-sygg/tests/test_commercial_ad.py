from __future__ import annotations

import binascii
import json
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import commercial_ad  # noqa: E402
from commercial_ad import (  # noqa: E402
    AdOrchestrator,
    VisualPromptBuilder,
    balanced_keep_durations,
    materialize_shot_timeline,
)
from providers import ProviderError, SubmissionUnknown, ValidationError  # noqa: E402


def png_bytes(seed: int, *, width: int = 17, height: int = 16, comment: str = "") -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)

    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            value = (x * (17 + seed * 3) + y * (11 + seed * 5) + ((x * y + seed * 13) % 47)) % 256
            row.extend((value, (value + seed * 29) % 256, (255 - value + seed * 7) % 256))
        rows.append(bytes(row))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    metadata = chunk(b"tEXt", f"Comment\x00{comment}".encode("utf-8")) if comment else b""
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + metadata
        + chunk(b"IEND", b"")
    )


PNG_BYTES = png_bytes(1)


def sample_analysis(source_images: list[str] | None = None) -> dict:
    return {
        "source_images": source_images if source_images is not None else ["/absolute/example-product.png"],
        "product_type": "便携音箱",
        "visible_features": ["圆角矩形机身", "顶部三个按键", "正面织物网罩"],
        "materials": ["哑光塑料", "织物"],
        "colors": ["#111111", "#C0C0C0"],
        "logo_text": "SYGG",
        "uncertainties": ["背面接口不可见"],
    }


def analysis_with_source(root: Path, *, seed: int = 900) -> dict:
    source = root / "source-product.png"
    source.write_bytes(png_bytes(seed))
    return sample_analysis([str(source.resolve())])


def shot_spec(
    visual: str,
    camera: str,
    transition: str,
    *,
    purpose: str = "清楚推进本段叙事",
    action: str = "商品完成一个明确可见的简单动作",
    product_detail: str = "保持商品结构、材质与可见标识清楚",
) -> dict:
    return {
        "purpose": purpose,
        "visual": visual,
        "action": action,
        "product_detail": product_detail,
        "camera": camera,
        "transition": transition,
    }


def sample_plan(clip_count: int = 1, narration: bool = False, human: bool = False) -> dict:
    talent_strategy = (
        {
            "mode": "human_interaction",
            "reason": "便携音箱需要通过手持操作展示尺度和按键交互",
            "framing": "partial_body",
            "adult_only": True,
            "persona": "冷幽默、专注的年轻科技主理人",
            "wardrobe": "现代黑色机能夹克",
            "grooming": "干净冷峻的自然妆造",
            "identity_anchor": "同一成年人物、同一脸型发型肤色体型和服装",
            "interaction_actions": ["单手拿起音箱", "指尖按下顶部按键"],
        }
        if human
        else {
            "mode": "pure_product",
            "reason": "产品结构与材质可以独立说明核心价值",
            "framing": "none",
            "interaction_actions": [],
        }
    )
    return {
        "big_idea": "让声音拥有可见的能量",
        "aspect_ratio": "9:16",
        "aspect_ratio_reason": "移动端短视频信息流需要竖屏构图",
        "style": "cyber_holographic_vfx",
        "style_rationale": "冷蓝全息能量能把音箱的声音表现为可见、精密的动态",
        "story_arc": "从轮廓唤醒到能量释放，再以完整商品英雄镜头建立记忆",
        "palette": {"primary": "#07111F", "secondary": "#00E5FF", "rim": "#7A5CFF"},
        "audiovisual_tone": "冷峻、精密、低频冲击",
        "global_anchor": "深色镜面空间，右后方青色轮廓光，产品始终位于中心",
        "ad_copy": ["能量，听得见"],
        "talent_strategy": talent_strategy,
        "narration": {
            "enabled": narration,
            "user_requested": narration,
            "text": "让每一次播放，都释放清晰能量。" if narration else "",
            "voice_id": "Chinese (Mandarin)_Reliable_Executive" if narration else "",
            "voice_name": "沉稳高管" if narration else "",
            "reason": "科技产品需要可信、稳定的声音" if narration else "本测试方案以产品动作和环境音表达，不需要旁白",
        },
        "clips": [
            {
                "index": index,
                "narrative_role": f"第 {index} 段承担独立且不重复的叙事推进",
                "storyboard_reason": "三段式产品广告按开场、核心动作、英雄定格形成清晰视觉顺序",
                "shots": [
                    shot_spec(
                        f"暗场轮廓光揭示第 {index} 段商品",
                        "慢速微距推进",
                        "光扫匹配切",
                        purpose="开场钩子",
                        action="轮廓光扫过机身并显露正面",
                        product_detail="圆角轮廓、正面织物网罩和顶部按键位置",
                    ),
                    shot_spec(
                        f"商品完成第 {index} 道能量环与核心卖点动作",
                        "Crash Zoom 后小幅环绕",
                        "能量环扩散匹配切",
                        purpose="核心卖点可视化",
                        action="能量环从网罩中心释放并环绕商品",
                        product_detail="织物纹理、哑光机身和按键保持完整",
                    ),
                    shot_spec(
                        f"完整商品进入第 {index} 段英雄定格",
                        "平滑拉出至正面中心",
                        "产品正面居中并稳定定格",
                        purpose="品牌英雄收束",
                        action="特效收束，商品稳定悬停",
                        product_detail="完整轮廓、Logo位置和主要材质",
                    ),
                ],
                "visual_progression": f"产品旋转并释放第 {index} 道能量环",
                "talent_action": "单手拿起音箱并以指尖按下顶部按键" if human else "",
                "camera": "微距推进后环绕",
                "sfx": "快节奏重鼓点、机械咔哒与能量涌动",
                "transition_in": "从黑场或上一段青色能量环进入",
                "transition_out": "产品正面居中，青色能量环扩散至满屏",
            }
            for index in range(1, clip_count + 1)
        ],
    }


class DurationAndPromptTests(unittest.TestCase):
    def test_balanced_duration_plan_always_uses_max_upstream_duration(self) -> None:
        self.assertEqual(balanced_keep_durations(10), [10])
        self.assertEqual(balanced_keep_durations(15), [8, 7])
        self.assertEqual(balanced_keep_durations(20), [10, 10])
        self.assertEqual(balanced_keep_durations(25), [9, 8, 8])

    def test_prepare_requires_at_least_one_real_source_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            orchestrator = AdOrchestrator.create("missing-source", Path(temp))
            with self.assertRaisesRegex(ValidationError, "source_images must contain 1 to 6"):
                orchestrator.prepare(sample_analysis([]), sample_plan(), target_duration=10)

    def test_prompt_contains_visual_and_safety_constraints(self) -> None:
        plan = sample_plan()
        builder = VisualPromptBuilder(sample_analysis(), plan)
        clip = {
            "index": 1,
            "keep_duration": 8,
            "storyboard_layout": "single_view",
            "storyboard_panel_count": 1,
            "storyboard_reason": "连续镜头最适合展示完整商品",
            "narrative_role": "以连续镜头完成商品识别与英雄收束",
            "shots": materialize_shot_timeline(
                [shot_spec("商品英雄构图", "平滑环绕", "闪白")], 7.2
            ),
            "visual_progression": "产品旋转",
            "camera": "环绕",
            "sfx": "机械咔哒与能量涌动",
            "transition_in": "黑场进入",
            "transition_out": "闪白",
        }
        storyboard = builder.storyboard_prompt(clip)
        omni = builder.omni_prompt(clip)
        self.assertIn("Octane Render", storyboard)
        self.assertEqual(omni.count(commercial_ad.SILENT_CONSTRAINT), 1)
        self.assertIn("no person", omni)
        self.assertNotIn("Whip Pan", omni)
        self.assertNotIn("liquid mercury chrome", omni)
        self.assertIn("storyboard grid", omni)
        self.assertIn("No narration subtitles", omni)
        self.assertIn("single unbroken scene", omni)
        self.assertIn("not literal initial frames", omni)
        self.assertNotIn("6-panel", storyboard)
        self.assertIn("不要分格", storyboard)

    def test_product_master_prompt_removes_source_photo_scene(self) -> None:
        prompt = VisualPromptBuilder(sample_analysis(), sample_plan()).product_master_prompt()
        self.assertIn("product-mockup", prompt)
        self.assertIn("never copy the source background", prompt)
        self.assertIn("not the final ad keyframe", prompt)
        self.assertIn("no advertising copy", prompt)

    def test_human_prompt_locks_adult_talent_and_interaction(self) -> None:
        plan = sample_plan(human=True)
        builder = VisualPromptBuilder(sample_analysis(), plan)
        clip = {
            "index": 1,
            "keep_duration": 10,
            "storyboard_layout": "single_view",
            "storyboard_panel_count": 1,
            "storyboard_reason": "人物与商品交互用连续镜头保持动作可信",
            "narrative_role": "用人物操作证明商品尺度与按键交互",
            "shots": materialize_shot_timeline(
                [
                    shot_spec(
                        "人物拿起音箱并操作顶部按键",
                        "Crash Zoom 后环绕",
                        "能量环闪白",
                        purpose="人物交互证明",
                        action="成年人单手拿起音箱并按下顶部按键",
                        product_detail="手持尺度、顶部按键与织物网罩",
                    )
                ],
                9.2,
            ),
            "visual_progression": "人物拿起音箱并操作顶部按键",
            "talent_action": "单手拿起音箱并以指尖按下顶部按键",
            "camera": "Crash Zoom 后环绕",
            "sfx": "机械咔哒与能量涌动",
            "transition_in": "黑场人物手部入画",
            "transition_out": "能量环闪白",
        }
        prompt = builder.omni_prompt(clip)
        talent_reference = builder.talent_reference_prompt()
        self.assertIsNotNone(talent_reference)
        self.assertIn("clearly adult, matching the age in the approved persona", talent_reference)
        self.assertNotIn("age 22-35", talent_reference)
        self.assertIn("no product, no text", talent_reference)
        self.assertIn("clearly adult person", prompt)
        self.assertIn("from Image2", prompt)
        self.assertIn("no speaking gestures", prompt)
        self.assertIn("单手拿起音箱", prompt)

    def test_three_panel_prompt_has_sequential_timecodes_and_no_split_screen(self) -> None:
        plan = sample_plan()
        plan["clips"][0]["storyboard_reason"] = "三个明确阶段需要按时序展示"
        plan["clips"][0]["shots"] = [
            shot_spec(
                "轮廓光揭示商品",
                "慢推",
                "光扫",
                purpose="钩子",
                action="轮廓光扫过机身并显露正面",
                product_detail="圆角轮廓、正面织物网罩和顶部按键位置",
            ),
            shot_spec("产品爆炸图解构", "微距推进", "匹配切", purpose="结构证明"),
            shot_spec("完整产品英雄定格", "平滑拉出", "中心定格", purpose="英雄收束"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orchestrator = AdOrchestrator.create("three-panel", root)
            state = orchestrator.prepare(analysis_with_source(root), plan, target_duration=10)
        clip = state["clips"][0]
        self.assertEqual(clip["storyboard_panel_count"], 3)
        self.assertIn("3 个连续画面", clip["storyboard_prompt"])
        self.assertIn("uploaded to Omni unchanged", clip["storyboard_prompt"])
        self.assertIn("Image1: story order and composition", clip["omni_prompt"])
        self.assertIn("Image2: product identity only", clip["omni_prompt"])
        self.assertNotIn("<IMAGE_REF_1>", clip["omni_prompt"])
        self.assertLess(len(clip["omni_prompt"]), 3000)
        self.assertIn("[0.00-3.07s]", clip["omni_prompt"])
        self.assertIn("[6.13-9.20s]", clip["omni_prompt"])
        self.assertNotIn("OVERALL STORY ARC", clip["omni_prompt"])
        self.assertIn("action: 轮廓光扫过机身并显露正面", clip["omni_prompt"])
        self.assertIn("detail: 圆角轮廓、正面织物网罩和顶部按键位置", clip["omni_prompt"])
        self.assertIn("storyboard grid", clip["omni_prompt"])

    def test_six_shot_storyboard_is_rejected(self) -> None:
        plan = sample_plan()
        plan["clips"][0]["shots"] = [
            shot_spec(f"阶段 {index}", "推进", "匹配切")
            for index in range(6)
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orchestrator = AdOrchestrator.create("six-panel", root)
            with self.assertRaisesRegex(ValidationError, "1 to 4 shots under this workflow"):
                orchestrator.prepare(analysis_with_source(root), plan, target_duration=10)

    def test_four_panel_supports_approved_fast_timing(self) -> None:
        plan = sample_plan()
        plan["clips"][0]["shots"] = [
            shot_spec(f"阶段 {index}", "推进", "匹配切")
            for index in range(4)
        ]
        plan["clips"][0]["high_density_simple_actions"] = True
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orchestrator = AdOrchestrator.create("too-dense", root)
            state = orchestrator.prepare(analysis_with_source(root), plan, target_duration=8)
            self.assertEqual(state["clips"][0]["storyboard_panel_count"], 4)
            self.assertAlmostEqual(state["clips"][0]["shots"][0]["end"], 1.8)

    def test_one_panel_needs_no_redundant_flag(self) -> None:
        plan = sample_plan()
        plan["clips"][0]["shots"] = [plan["clips"][0]["shots"][0]]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orchestrator = AdOrchestrator.create("unintentional-one-shot", root)
            state = orchestrator.prepare(analysis_with_source(root), plan, target_duration=10)
            self.assertEqual(state["clips"][0]["storyboard_panel_count"], 1)

    def test_one_panel_is_allowed_when_intentional_and_explained(self) -> None:
        plan = sample_plan()
        plan["clips"][0]["shots"] = [plan["clips"][0]["shots"][0]]
        plan["clips"][0]["intentional_one_shot"] = True
        plan["clips"][0]["one_shot_reason"] = "慢速流体解构需要一条连续电影长镜头"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orchestrator = AdOrchestrator.create("intentional-one-shot", root)
            state = orchestrator.prepare(analysis_with_source(root), plan, target_duration=10)
        self.assertEqual(state["clips"][0]["storyboard_panel_count"], 1)
        self.assertIn("single unbroken scene", state["clips"][0]["omni_prompt"])

    def test_four_panel_needs_no_redundant_flag(self) -> None:
        plan = sample_plan()
        plan["clips"][0]["shots"] = [
            shot_spec(f"简单阶段 {index}", "小幅推进", "匹配切")
            for index in range(4)
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orchestrator = AdOrchestrator.create("unguarded-four-panel", root)
            state = orchestrator.prepare(analysis_with_source(root), plan, target_duration=10)
            self.assertEqual(state["clips"][0]["storyboard_panel_count"], 4)

    def test_pure_product_mode_does_not_create_talent_reference_prompt(self) -> None:
        builder = VisualPromptBuilder(sample_analysis(), sample_plan())
        self.assertIsNone(builder.talent_reference_prompt())

    def test_human_mode_rejects_non_adult_plan(self) -> None:
        plan = sample_plan(human=True)
        plan["talent_strategy"]["adult_only"] = False
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orchestrator = AdOrchestrator.create("invalid-human", root)
            with self.assertRaisesRegex(ValidationError, "adult_only=true"):
                orchestrator.prepare(analysis_with_source(root), plan, target_duration=10)


class StateWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _project(self, duration: int = 10, narration: bool = False, human: bool = False) -> AdOrchestrator:
        count = len(balanced_keep_durations(duration))
        orchestrator = AdOrchestrator.create("demo", self.root / "outputs")
        orchestrator.prepare(
            analysis_with_source(self.root), sample_plan(count, narration, human), target_duration=duration
        )
        return orchestrator

    def _reference_rows(
        self, orchestrator: AdOrchestrator, clip_count: int
    ) -> tuple[Path, list[Path], list[dict]]:
        reference_dir = orchestrator.project_dir / "references"
        product = reference_dir / "product-master.png"
        product.write_bytes(png_bytes(1, width=9, height=16))
        storyboards = []
        state = orchestrator.load()
        rows = [
            {
                "role": "product_master",
                "path": str(product.resolve()),
                "origin": "codex_imagegen",
                "derived_from_source_hashes": [row["sha256"] for row in state["source_assets"]],
                "identity_verified": True,
            }
        ]
        for index in range(1, clip_count + 1):
            storyboard = reference_dir / f"storyboard-{index}.png"
            panel_count = state["clips"][index - 1]["storyboard_panel_count"]
            storyboard.write_bytes(png_bytes(100 + index, width=9 * panel_count, height=16))
            storyboards.append(storyboard)
            rows.append(
                {
                    "role": "storyboard",
                    "clip_index": index,
                    "path": str(storyboard.resolve()),
                    "origin": "codex_imagegen",
                    "derived_from_product_master_sha256": commercial_ad.sha256_file(product),
                    "panel_count": panel_count,
                    "clean_for_video": True,
                    "panel_order_verified": True,
                    "distinct_panels_verified": True,
                }
            )
        return product, storyboards, rows

    def _register(self, orchestrator: AdOrchestrator, clip_count: int) -> tuple[Path, list[Path]]:
        product, storyboards, rows = self._reference_rows(orchestrator, clip_count)
        orchestrator.approve_plan()
        orchestrator.register_references({"references": rows})
        return product, storyboards

    def test_prepare_uses_approved_vertical_aspect_and_fixed_omni_contract(self) -> None:
        orchestrator = self._project()
        state = orchestrator.load()
        self.assertEqual(state["state"], "awaiting_plan_approval")
        self.assertEqual(state["schema_version"], 10)
        self.assertEqual(state["target"]["width"], 720)
        self.assertEqual(state["target"]["height"], 1280)
        self.assertEqual(state["target"]["resolution"], "720p")
        self.assertEqual(state["target"]["aspect_ratio"], "9:16")
        self.assertEqual(state["paid_counts"], {"omni": 1, "minimax": 0})
        self.assertEqual(state["clips"][0]["upstream_duration"], 10)
        self.assertEqual(state["provider_contract"]["model"], "omni-fast-no-water")
        self.assertEqual(state["provider_contract"]["provider_model"], "omni-flash")
        self.assertEqual(state["source_assets"][0]["usage"], "imagegen_evidence_only")
        self.assertIn("not the final ad keyframe", state["product_master_prompt"])
        self.assertEqual(state["clips"][0]["storyboard_panel_count"], 3)
        self.assertEqual(state["reference_asset_plan"]["generation_order"], ["product_master", "storyboard:1"])
        self.assertEqual(state["reference_asset_plan"]["minimum_imagegen_calls"], 2)

    def test_prepare_supports_horizontal_720p_end_to_end_contract(self) -> None:
        plan = sample_plan()
        plan["aspect_ratio"] = "16:9"
        plan["aspect_ratio_reason"] = "官网横幅与大屏发布需要横屏构图"
        orchestrator = AdOrchestrator.create("horizontal", self.root / "outputs")
        state = orchestrator.prepare(analysis_with_source(self.root, seed=906), plan, target_duration=10)
        self.assertEqual(state["target"]["aspect_ratio"], "16:9")
        self.assertEqual((state["target"]["width"], state["target"]["height"]), (1280, 720))
        self.assertEqual(state["provider_contract"]["aspect_ratio"], "16:9")
        self.assertIn("premium 16:9 commercial", state["clips"][0]["omni_prompt"])
        self.assertIn("Do not constrain its canvas to the final video ratio", state["product_master_prompt"])

    def test_prepare_can_freeze_explicit_cangyuan_provider(self) -> None:
        orchestrator = AdOrchestrator.create("direct-cangyuan", self.root / "outputs")
        state = orchestrator.prepare(
            analysis_with_source(self.root, seed=920),
            sample_plan(),
            target_duration=10,
            video_provider="cangyuan",
        )
        self.assertEqual(state["provider_contract"]["provider"], "cangyuan")
        self.assertEqual(state["provider_contract"]["provider_model"], "omni-fast-no-water")
        self.assertEqual(state["provider_contract"]["selection"], "explicit_user_authorization")
        self.assertFalse(state["provider_contract"]["fallback"]["enabled"])

    def test_prepare_rejects_unsupported_or_unexplained_aspect_ratio(self) -> None:
        plan = sample_plan()
        plan["aspect_ratio"] = "1:1"
        with self.assertRaisesRegex(ValidationError, "9:16 or 16:9"):
            AdOrchestrator.create("square", self.root / "outputs").prepare(
                analysis_with_source(self.root, seed=907), plan, target_duration=10
            )
        plan = sample_plan()
        plan["aspect_ratio_reason"] = ""
        with self.assertRaisesRegex(ValidationError, "aspect_ratio_reason"):
            AdOrchestrator.create("no-aspect-reason", self.root / "outputs").prepare(
                analysis_with_source(self.root, seed=908), plan, target_duration=10
            )

    def test_prepare_requires_director_story_metadata(self) -> None:
        plan = sample_plan()
        plan.pop("style_rationale")
        with self.assertRaisesRegex(ValidationError, "style_rationale"):
            AdOrchestrator.create("no-style-reason", self.root / "outputs").prepare(
                analysis_with_source(self.root, seed=909), plan, target_duration=10
            )
        plan = sample_plan()
        plan["clips"][0]["shots"][0].pop("product_detail")
        with self.assertRaisesRegex(ValidationError, "product_detail"):
            AdOrchestrator.create("no-product-detail", self.root / "outputs").prepare(
                analysis_with_source(self.root, seed=910), plan, target_duration=10
            )

    def test_twenty_second_plan_has_one_global_story_map_and_two_clip_timelines(self) -> None:
        orchestrator = AdOrchestrator.create("twenty-second-map", self.root / "outputs")
        state = orchestrator.prepare(
            analysis_with_source(self.root, seed=911), sample_plan(2), target_duration=20
        )
        self.assertEqual(
            [(clip["global_start"], clip["global_end"]) for clip in state["clips"]],
            [(0.0, 10.0), (10.0, 20.0)],
        )
        report = (orchestrator.project_dir / "plan.md").read_text(encoding="utf-8")
        self.assertIn("全片分段地图", report)
        self.assertIn("0.00–10.00s", report)
        self.assertIn("10.00–20.00s", report)
        self.assertIn("叙事任务", report)
        self.assertIn("可见动作", report)
        self.assertIn("商品细节", report)

    def test_twenty_second_eight_beat_story_uses_two_distinct_four_panel_sheets(self) -> None:
        plan = sample_plan(2)
        for clip_index, clip in enumerate(plan["clips"], start=1):
            clip["shots"] = [
                shot_spec(
                    f"视频段 {clip_index} 的独立画面 {shot_index}",
                    "短促推进",
                    "动作匹配切",
                    purpose=f"视频段 {clip_index} 的任务 {shot_index}",
                    action=f"商品完成视频段 {clip_index} 的简单动作 {shot_index}",
                    product_detail=f"视频段 {clip_index} 的真实商品细节 {shot_index}",
                )
                for shot_index in range(1, 5)
            ]
            clip["high_density_simple_actions"] = True
        orchestrator = AdOrchestrator.create("twenty-second-eight-beat", self.root / "outputs")
        state = orchestrator.prepare(
            analysis_with_source(self.root, seed=914), plan, target_duration=20
        )
        self.assertEqual([clip["storyboard_panel_count"] for clip in state["clips"]], [4, 4])
        self.assertEqual(
            [clip["storyboard_reading_order"] for clip in state["clips"]],
            [
                "top-left, top-right, bottom-left, bottom-right",
                "top-left, top-right, bottom-left, bottom-right",
            ],
        )
        self.assertEqual(
            state["reference_asset_plan"]["generation_order"],
            ["product_master", "storyboard:1", "storyboard:2"],
        )
        self.assertNotEqual(
            state["clips"][0]["shots"][0]["visual"],
            state["clips"][1]["shots"][0]["visual"],
        )

    def test_duplicate_clip_narrative_role_is_rejected_before_image_generation(self) -> None:
        plan = sample_plan(2)
        plan["clips"][1]["narrative_role"] = plan["clips"][0]["narrative_role"]
        with self.assertRaisesRegex(ValidationError, "narrative_role must be distinct"):
            AdOrchestrator.create("duplicate-clip-role", self.root / "outputs").prepare(
                analysis_with_source(self.root, seed=912), plan, target_duration=20
            )

    def test_duplicate_clip_shot_blueprint_is_rejected_before_image_generation(self) -> None:
        plan = sample_plan(2)
        plan["clips"][1]["shots"] = [dict(shot) for shot in plan["clips"][0]["shots"]]
        with self.assertRaisesRegex(ValidationError, "repeat the same shot blueprint"):
            AdOrchestrator.create("duplicate-clip-shots", self.root / "outputs").prepare(
                analysis_with_source(self.root, seed=913), plan, target_duration=20
            )

    def test_prepare_cannot_overwrite_a_project_after_plan_approval(self) -> None:
        orchestrator = self._project()
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "new project-id"):
            orchestrator.prepare(analysis_with_source(self.root, seed=901), sample_plan(), target_duration=10)

    def test_changed_source_image_blocks_plan_approval(self) -> None:
        orchestrator = self._project()
        source = Path(orchestrator.load()["source_assets"][0]["path"])
        source.write_bytes(png_bytes(999))
        with self.assertRaisesRegex(ValidationError, "Frozen source image is missing or changed"):
            orchestrator.approve_plan()

    def test_legacy_project_schema_fails_with_clear_rebuild_message(self) -> None:
        orchestrator = self._project()
        state = json.loads(orchestrator.state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 2
        orchestrator.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "Unsupported project schema"):
            orchestrator.load()

    def test_raw_product_role_is_rejected(self) -> None:
        orchestrator = self._project()
        raw_copy = orchestrator.project_dir / "references" / "raw-product.png"
        raw_copy.write_bytes(png_bytes(77))
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "Raw product references are forbidden"):
            orchestrator.register_references(
                {"references": [{"role": "product", "path": str(raw_copy.resolve())}]}
            )

    def test_raw_source_cannot_masquerade_as_product_master(self) -> None:
        orchestrator = self._project()
        state = orchestrator.load()
        source = Path(state["source_assets"][0]["path"])
        product_master = orchestrator.project_dir / "references" / "product-master.png"
        product_master.write_bytes(source.read_bytes())
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "raw source image or an identical-pixel copy"):
            orchestrator.register_references(
                {
                    "references": [
                        {
                            "role": "product_master",
                            "path": str(product_master.resolve()),
                            "origin": "codex_imagegen",
                            "derived_from_source_hashes": [row["sha256"] for row in state["source_assets"]],
                            "identity_verified": True,
                        }
                    ]
                }
            )

    def test_raw_source_cannot_enter_omni_through_style_role(self) -> None:
        orchestrator = self._project()
        state = orchestrator.load()
        source = Path(state["source_assets"][0]["path"])
        _, _, rows = self._reference_rows(orchestrator, 1)
        fake_style = orchestrator.project_dir / "references" / "style.png"
        fake_style.write_bytes(source.read_bytes())
        rows.append({"role": "style", "path": str(fake_style.resolve()), "origin": "user_provided"})
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "cannot be registered as style or sent to Omni"):
            orchestrator.register_references({"references": rows})

    def test_source_images_are_not_in_omni_public_reference_list(self) -> None:
        orchestrator = self._project()
        self._register(orchestrator, 1)
        state = orchestrator.load()
        public_files = commercial_ad._unique_approved_files(state["reference_sets"])
        public_paths = {row["path"] for row in public_files}
        source_paths = {row["path"] for row in state["source_assets"]}
        self.assertTrue(public_paths)
        self.assertTrue(public_paths.isdisjoint(source_paths))

    def test_omni_receives_each_approved_storyboard_unchanged(self) -> None:
        orchestrator = self._project(duration=15)
        product, _ = self._register(orchestrator, 2)
        style = orchestrator.project_dir / "references" / "style.png"
        style.write_bytes(png_bytes(50))
        current = orchestrator.load()
        rows = current["references"] + [
            {"role": "style", "path": str(style.resolve()), "origin": "codex_imagegen"}
        ]
        state = orchestrator.register_references({"references": rows})
        self.assertEqual([row["role"] for row in state["reference_sets"]["1"]], ["storyboard_reference", "product_master"])
        self.assertEqual(len(state["reference_sets"]["2"]), 2)
        self.assertEqual(state["reference_sets"]["1"][0]["path"], state["references"][1]["path"])
        self.assertEqual(state["reference_sets"]["1"][0]["sha256"], state["references"][1]["sha256"])
        self.assertEqual(state["reference_sets"]["1"][0]["reading_order"], "left to right in one row")
        self.assertIn("product_master", [row["role"] for row in state["reference_sets"]["1"]])
        self.assertEqual(current["state"], "awaiting_reference_approval")

    def test_reference_approval_report_shows_actual_omni_inputs(self) -> None:
        orchestrator = self._project()
        product, _ = self._register(orchestrator, 1)
        state = orchestrator.load()
        report_path = Path(state["artifacts"]["reference_approval"])
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("是否上传以下方实际清单为准", report)
        self.assertIn(str(product.resolve()), report)
        self.assertIn("Image1：storyboard_reference", report)
        self.assertIn("Image2：product_master", report)
        self.assertIn("用户原图绝不公开", report)
        self.assertIn("图片外导演标注", report)
        self.assertIn("开场钩子", report)
        self.assertIn("轮廓光扫过机身并显露正面", report)
        self.assertIn("圆角轮廓、正面织物网罩和顶部按键位置", report)

    def test_storyboard_lineage_and_panel_count_are_required(self) -> None:
        orchestrator = self._project()
        _, _, rows = self._reference_rows(orchestrator, 1)
        orchestrator.approve_plan()
        broken_lineage = [dict(row) for row in rows]
        broken_lineage[1]["derived_from_product_master_sha256"] = "wrong"
        with self.assertRaisesRegex(ValidationError, "generated from the approved product_master"):
            orchestrator.register_references({"references": broken_lineage})
        broken_panels = [dict(row) for row in rows]
        broken_panels[1]["panel_count"] = 1
        with self.assertRaisesRegex(ValidationError, "panel_count does not match"):
            orchestrator.register_references({"references": broken_panels})

    def test_product_master_canvas_ratio_is_not_a_hard_video_constraint(self) -> None:
        orchestrator = self._project()
        product, _, rows = self._reference_rows(orchestrator, 1)
        product.write_bytes(png_bytes(71, width=16, height=9))
        rows[1]["derived_from_product_master_sha256"] = commercial_ad.sha256_file(product)
        orchestrator.approve_plan()
        state = orchestrator.register_references({"references": rows})
        self.assertEqual(state["reference_sets"]["1"][0]["role"], "storyboard_reference")

    def test_multi_panel_storyboard_canvas_ratio_is_not_a_hard_constraint(self) -> None:
        orchestrator = self._project()
        _, storyboards, rows = self._reference_rows(orchestrator, 1)
        storyboards[0].write_bytes(png_bytes(72, width=9, height=16))
        orchestrator.approve_plan()
        state = orchestrator.register_references({"references": rows})
        self.assertEqual(state["reference_sets"]["1"][0]["width"], 9)
        self.assertEqual(state["reference_sets"]["1"][0]["height"], 16)

    def test_wide_three_panel_storyboard_is_uploaded_without_reframing(self) -> None:
        orchestrator = self._project()
        _, storyboards, rows = self._reference_rows(orchestrator, 1)
        storyboards[0].write_bytes(png_bytes(73, width=240, height=100))
        orchestrator.approve_plan()
        state = orchestrator.register_references({"references": rows})
        execution = state["reference_sets"]["1"][0]
        self.assertEqual((execution["width"], execution["height"]), (240, 100))
        self.assertEqual(execution["path"], state["references"][1]["path"])

    def test_storyboard_requires_explicit_visual_cleanliness_gate(self) -> None:
        orchestrator = self._project()
        _, _, rows = self._reference_rows(orchestrator, 1)
        rows[1].pop("clean_for_video")
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "clean_for_video=true"):
            orchestrator.register_references({"references": rows})

    def test_storyboard_requires_explicit_distinct_panel_review(self) -> None:
        orchestrator = self._project()
        _, _, rows = self._reference_rows(orchestrator, 1)
        rows[1].pop("distinct_panels_verified")
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "distinct_panels_verified=true"):
            orchestrator.register_references({"references": rows})

    def test_four_panel_uses_one_clean_sheet_without_dropping_a_beat(self) -> None:
        plan = sample_plan()
        plan["clips"][0]["shots"] = [
            shot_spec(f"简单阶段 {index}", "小幅推进", "匹配切")
            for index in range(1, 5)
        ]
        plan["clips"][0]["high_density_simple_actions"] = True
        orchestrator = AdOrchestrator.create("four-panel", self.root / "outputs")
        orchestrator.prepare(analysis_with_source(self.root, seed=905), plan, target_duration=10)
        _, _, rows = self._reference_rows(orchestrator, 1)
        orchestrator.approve_plan()
        state = orchestrator.register_references({"references": rows})
        self.assertEqual(len(state["reference_sets"]["1"]), 2)
        self.assertEqual(state["reference_sets"]["1"][0]["role"], "storyboard_reference")
        self.assertIn("top-left, top-right, bottom-left, bottom-right", state["clips"][0]["omni_prompt"])

    def test_human_mode_requires_and_orders_global_talent_reference(self) -> None:
        orchestrator = self._project(human=True)
        self.assertEqual(
            orchestrator.load()["reference_asset_plan"]["generation_order"],
            ["product_master", "talent", "storyboard:1"],
        )
        _, _, rows = self._reference_rows(orchestrator, 1)
        talent = orchestrator.project_dir / "references" / "adult-talent.png"
        talent.write_bytes(png_bytes(60))
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "requires exactly one global talent"):
            orchestrator.register_references({"references": rows})
        for row in rows:
            if row["role"] == "storyboard":
                row["derived_from_talent_sha256"] = commercial_ad.sha256_file(talent)
        state = orchestrator.register_references(
            {
                "references": rows
                + [{"role": "talent", "path": str(talent.resolve()), "origin": "codex_imagegen"}]
            }
        )
        self.assertEqual([row["role"] for row in state["reference_sets"]["1"]], ["storyboard_reference", "product_master", "talent"])

    def test_pure_product_mode_rejects_talent_reference(self) -> None:
        orchestrator = self._project()
        _, _, rows = self._reference_rows(orchestrator, 1)
        talent = orchestrator.project_dir / "references" / "adult-talent.png"
        talent.write_bytes(png_bytes(60))
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "must not include a talent"):
            orchestrator.register_references(
                {
                    "references": rows
                    + [{"role": "talent", "path": str(talent.resolve()), "origin": "codex_imagegen"}]
                }
            )

    def test_human_mode_rejects_style_as_a_fourth_reference(self) -> None:
        orchestrator = self._project(human=True)
        _, _, rows = self._reference_rows(orchestrator, 1)
        paths = {name: orchestrator.project_dir / "references" / f"{name}.png" for name in ("talent", "style")}
        paths["talent"].write_bytes(png_bytes(60))
        paths["style"].write_bytes(png_bytes(80))
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "encode style in the storyboard"):
            orchestrator.register_references(
                {
                    "references": rows
                    + [
                        {"role": "talent", "path": str(paths["talent"].resolve()), "origin": "codex_imagegen"},
                        {"role": "style", "path": str(paths["style"].resolve()), "origin": "codex_imagegen"},
                    ]
                }
            )

    def test_changed_reference_invalidates_final_approval(self) -> None:
        orchestrator = self._project()
        product, _ = self._register(orchestrator, 1)
        orchestrator.approve_references()
        product.write_bytes(b"changed")
        with self.assertRaisesRegex(ValidationError, "missing or changed"):
            orchestrator.preflight(verify_tunnel=False)

    def test_changed_plan_invalidates_reference_approval(self) -> None:
        orchestrator = self._project()
        self._register(orchestrator, 1)
        orchestrator.approve_references()
        state = orchestrator.load()
        state["plan"]["big_idea"] = "被修改的方案"
        orchestrator.save(state)
        with self.assertRaisesRegex(ValidationError, "Plan changed"):
            orchestrator.preflight(verify_tunnel=False)

    def test_reference_limit_requires_one_storyboard_per_clip(self) -> None:
        orchestrator = self._project()
        product, _, rows = self._reference_rows(orchestrator, 1)
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "exactly one storyboard"):
            orchestrator.register_references(
                {"references": [rows[0]]}
            )

    def test_non_image_reference_is_rejected_before_approval(self) -> None:
        orchestrator = self._project()
        product = orchestrator.project_dir / "references" / "product-master.png"
        product.write_bytes(b"not-an-image")
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "PNG, JPEG, or WebP"):
            orchestrator.register_references(
                {
                    "references": [
                        {
                            "role": "product_master",
                            "path": str(product.resolve()),
                            "origin": "codex_imagegen",
                            "derived_from_source_hashes": [
                                row["sha256"] for row in orchestrator.load()["source_assets"]
                            ],
                        }
                    ]
                }
            )

    def test_exact_duplicate_reference_files_are_rejected(self) -> None:
        orchestrator = self._project()
        reference_dir = orchestrator.project_dir / "references"
        product = reference_dir / "product-master.png"
        storyboard = reference_dir / "storyboard.png"
        product.write_bytes(PNG_BYTES)
        storyboard.write_bytes(PNG_BYTES)
        state = orchestrator.load()
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "exact duplicates"):
            orchestrator.register_references(
                {
                    "references": [
                        {
                            "role": "product_master",
                            "path": str(product.resolve()),
                            "origin": "codex_imagegen",
                            "derived_from_source_hashes": [row["sha256"] for row in state["source_assets"]],
                            "identity_verified": True,
                        },
                        {
                            "role": "storyboard",
                            "clip_index": 1,
                            "path": str(storyboard.resolve()),
                            "origin": "codex_imagegen",
                            "derived_from_product_master_sha256": commercial_ad.sha256_file(product),
                            "panel_count": 3,
                            "clean_for_video": True,
                            "panel_order_verified": True,
                            "distinct_panels_verified": True,
                        },
                    ]
                }
            )

    def test_storyboard_cannot_be_a_reencoded_product_master(self) -> None:
        orchestrator = self._project()
        reference_dir = orchestrator.project_dir / "references"
        product = reference_dir / "product-master.png"
        storyboard = reference_dir / "storyboard.png"
        product.write_bytes(png_bytes(41, width=9, height=16, comment="master encoding"))
        storyboard.write_bytes(png_bytes(41, width=9, height=16, comment="storyboard encoding"))
        state = orchestrator.load()
        product_hash = commercial_ad.sha256_file(product)
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "identical decoded pixels"):
            orchestrator.register_references(
                {
                    "references": [
                        {
                            "role": "product_master",
                            "path": str(product.resolve()),
                            "origin": "codex_imagegen",
                            "derived_from_source_hashes": [row["sha256"] for row in state["source_assets"]],
                            "identity_verified": True,
                        },
                        {
                            "role": "storyboard",
                            "clip_index": 1,
                            "path": str(storyboard.resolve()),
                            "origin": "codex_imagegen",
                            "derived_from_product_master_sha256": product_hash,
                            "panel_count": 3,
                            "clean_for_video": True,
                            "panel_order_verified": True,
                            "distinct_panels_verified": True,
                        },
                    ]
                }
            )

    def test_visually_near_duplicate_storyboards_are_rejected(self) -> None:
        orchestrator = self._project(duration=20)
        reference_dir = orchestrator.project_dir / "references"
        product = reference_dir / "product-master.png"
        first = reference_dir / "storyboard-1.png"
        second = reference_dir / "storyboard-2.png"
        product.write_bytes(png_bytes(1, width=9, height=16))
        first.write_bytes(png_bytes(200, comment="first encoding"))
        second.write_bytes(png_bytes(200, comment="second encoding"))
        state = orchestrator.load()
        orchestrator.approve_plan()
        with self.assertRaisesRegex(ValidationError, "identical decoded pixels"):
            orchestrator.register_references(
                {
                    "references": [
                        {
                            "role": "product_master",
                            "path": str(product.resolve()),
                            "origin": "codex_imagegen",
                            "derived_from_source_hashes": [row["sha256"] for row in state["source_assets"]],
                            "identity_verified": True,
                        },
                        {
                            "role": "storyboard",
                            "clip_index": 1,
                            "path": str(first.resolve()),
                            "origin": "codex_imagegen",
                            "derived_from_product_master_sha256": commercial_ad.sha256_file(product),
                            "panel_count": 3,
                            "clean_for_video": True,
                            "panel_order_verified": True,
                            "distinct_panels_verified": True,
                        },
                        {
                            "role": "storyboard",
                            "clip_index": 2,
                            "path": str(second.resolve()),
                            "origin": "codex_imagegen",
                            "derived_from_product_master_sha256": commercial_ad.sha256_file(product),
                            "panel_count": 3,
                            "clean_for_video": True,
                            "panel_order_verified": True,
                            "distinct_panels_verified": True,
                        },
                    ]
                }
            )

    def test_resume_uses_task_id_without_submitting(self) -> None:
        orchestrator = self._project()
        self._register(orchestrator, 1)
        orchestrator.approve_references()
        state = orchestrator.load()
        state["state"] = "generating"
        orchestrator.save(state)
        orchestrator.ledger.append(
            provider="omni", event="submitted", request_hash="abc", task_id="task-1", clip_index=1
        )

        class FakeOmni:
            submit_called = False

            def __init__(self, api_key, ledger):
                self.ledger = ledger

            def submit(self, *args, **kwargs):
                type(self).submit_called = True
                raise AssertionError("resume must not submit")

            def poll(self, task_id):
                return {"status": "completed", "video_url": "https://example.com/video.mp4"}

            def download_result(self, task_id, data, output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"mock-video")
                return output

        orchestrator.omni_factory = FakeOmni
        orchestrator.key_loader = lambda service, account: "not-a-real-key"
        with mock.patch.object(orchestrator, "_assemble", return_value={"status": "resumed"}) as assemble:
            result = orchestrator.resume()
        self.assertEqual(result["status"], "resumed")
        self.assertFalse(FakeOmni.submit_called)
        assemble.assert_called_once()

    def test_assemble_uses_balanced_trim_plan_for_15_20_25_seconds(self) -> None:
        for duration, expected in ((15, [8, 7]), (20, [10, 10]), (25, [9, 8, 8])):
            with self.subTest(duration=duration):
                project = AdOrchestrator.create(f"demo-{duration}", self.root / "outputs")
                project.prepare(
                    analysis_with_source(self.root, seed=900 + duration),
                    sample_plan(len(expected)),
                    target_duration=duration,
                )
                raw = []
                for index in range(1, len(expected) + 1):
                    source = project.project_dir / "clips" / "raw" / f"clip-{index:02d}.mp4"
                    source.write_bytes(b"raw")
                    raw.append(source)
                    project.ledger.append(provider="omni", event="submitted", task_id=f"task-{index}", clip_index=index)
                    project.ledger.append(provider="omni", event="downloaded", task_id=f"task-{index}", path=str(source), sha256=commercial_ad.sha256_file(source))

                def fake_normalize(source, output, keep_duration, *, target_width, target_height, align_frames=False):
                    self.assertEqual((target_width, target_height), (720, 1280))
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"normalized")
                    return output

                def fake_stitch(clips, output):
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"stitched")
                    return output

                with mock.patch.object(commercial_ad, "normalize_clip", side_effect=fake_normalize) as normalize, mock.patch.object(
                    commercial_ad, "stitch_clips", side_effect=fake_stitch
                ), mock.patch.object(commercial_ad, "extract_review_frames", return_value=[]), mock.patch.object(
                    commercial_ad, "write_qa_report", return_value={"status": "pass"}
                ), mock.patch.object(commercial_ad, "probe_media", return_value={"has_audio": True, "audio_duration": 10}):
                    project._assemble(project.load(), raw, None)
                self.assertEqual([call.args[2] for call in normalize.call_args_list], expected)

    def test_creative_review_is_required_before_delivered_state(self) -> None:
        orchestrator = self._project()
        self._register(orchestrator, 1)
        orchestrator.approve_references()
        state = orchestrator.load()
        state["state"] = "awaiting_manual_review"
        orchestrator.save(state)
        qa_dir = orchestrator.project_dir / "qa"
        qa_dir.mkdir(parents=True, exist_ok=True)
        (qa_dir / "qa-report.json").write_text(
            json.dumps({"technical_status": "pass", "status": "awaiting_manual_review"}),
            encoding="utf-8",
        )
        (orchestrator.project_dir / "delivery-manifest.json").write_text(
            json.dumps(
                {
                    "status": "awaiting_manual_review",
                    "project_id": "demo",
                    "final_video": "final.mp4",
                    "qa_status": "awaiting_manual_review",
                }
            ),
            encoding="utf-8",
        )
        final = orchestrator.project_dir / "final" / "review-fixture.mp4"
        final.write_bytes(b"already technically checked by fixture")
        qa_path = orchestrator.project_dir / "qa" / "qa-report.json"
        qa = json.loads(qa_path.read_text())
        qa["final_sha256"] = commercial_ad.sha256_file(final)
        qa_path.write_text(json.dumps(qa))
        manifest_path = orchestrator.project_dir / "delivery-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["final_video"] = str(final)
        manifest_path.write_text(json.dumps(manifest))
        commercial_ad.atomic_write_json(orchestrator.project_dir / "assembly.json", {
            "target": state["target"], "narration": None, "audio": state["provider_contract"]["audio"],
            "clips": [{"clip_index": 1, "path": str(final), "sha256": commercial_ad.sha256_file(final)}]})
        checks = {key: True for key in commercial_ad.CREATIVE_REVIEW_CHECKS}
        checks["storyboard_sequence_followed"] = False
        failed = orchestrator.complete_review({"reviewer": "codex-vision", "checks": checks})
        self.assertEqual(failed["status"], "awaiting_manual_review")
        checks["storyboard_sequence_followed"] = True
        passed = orchestrator.complete_review({"reviewer": "codex-vision", "checks": checks})
        self.assertEqual(passed["status"], "delivered")
        self.assertFalse(passed["manual_review_required"])

    def test_video_generation_uses_two_workers_and_batches(self) -> None:
        orchestrator = self._project(duration=25)
        self._register(orchestrator, 3)
        state = orchestrator.load()
        execution = [row for rows in state["reference_sets"].values() for row in rows]
        public = {row["path"]: f"https://assets.example.com/{index}.png" for index, row in enumerate(execution)}
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        class FakeOmni:
            active = 0
            maximum = 0
            submitted = []

            def __init__(self, api_key, ledger):
                pass

            def submit(self, prompt, urls, *, clip_index, aspect_ratio, reference_bindings=None, max_retries=0, retry_of=None):
                if aspect_ratio != "9:16":
                    raise AssertionError("unexpected aspect ratio")
                if len(urls) != 2 or [r["role"] for r in reference_bindings] != ["storyboard_reference", "product_master"]:
                    raise AssertionError("each clip must upload its approved ordered roles")
                with lock:
                    type(self).active += 1
                    type(self).maximum = max(type(self).maximum, type(self).active)
                    type(self).submitted.append(clip_index)
                return f"task-{clip_index}"

            def poll(self, task_id):
                if task_id in ("task-1", "task-2"):
                    barrier.wait(timeout=2)
                return {"status": "completed", "video_url": "https://assets.example.com/video.mp4"}

            def download_result(self, task_id, data, output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"video")
                with lock:
                    type(self).active -= 1
                return output

        orchestrator.omni_factory = FakeOmni
        orchestrator.key_loader = lambda service, account: "not-a-real-key"
        outputs = orchestrator._generate_video_batches(state, public)
        self.assertEqual(len(outputs), 3)
        self.assertEqual(FakeOmni.maximum, 2)
        self.assertEqual(sorted(FakeOmni.submitted), [1, 2, 3])

    def test_explicit_cangyuan_selection_skips_primary_provider(self) -> None:
        orchestrator = AdOrchestrator.create("direct-cangyuan-generation", self.root / "outputs")
        orchestrator.prepare(
            analysis_with_source(self.root, seed=921),
            sample_plan(),
            target_duration=10,
            video_provider="cangyuan",
        )
        self._register(orchestrator, 1)
        state = orchestrator.load()
        execution = [row for rows in state["reference_sets"].values() for row in rows]
        public = {row["path"]: f"https://assets.example.com/{index}.png" for index, row in enumerate(execution)}
        services = []

        class NeverPrimary:
            def __init__(self, *args, **kwargs):
                raise AssertionError("primary provider must not be initialized")

        class FakeCangyuan:
            def __init__(self, api_key, ledger):
                self.ledger = ledger

            def check_available(self):
                return None

            def submit(self, prompt, urls, *, clip_index, aspect_ratio, reference_bindings=None, max_retries=0, retry_of=None, fallback_of=None):
                return "task-cangyuan-1"

            def poll(self, task_id):
                return {"status": "completed", "video_url": "https://assets.example.com/video.mp4"}

            def download_result(self, task_id, data, output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"video")
                return output

        def load_key(service, account):
            services.append(service)
            return "not-a-real-key"

        orchestrator.omni_factory = NeverPrimary
        orchestrator.fallback_factory = FakeCangyuan
        orchestrator.key_loader = load_key
        outputs = orchestrator._generate_video_batches(state, public)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(services, [commercial_ad.CANGYUAN_KEYCHAIN_SERVICE])
        self.assertTrue(any(
            row.get("event") == "fallback_selected"
            and row.get("reason") == "explicit_user_authorization"
            for row in orchestrator.ledger.records()
        ))

    def test_submission_unknown_in_first_batch_stops_before_third_clip(self) -> None:
        orchestrator = self._project(duration=25)
        self._register(orchestrator, 3)
        state = orchestrator.load()
        execution = [row for rows in state["reference_sets"].values() for row in rows]
        public = {row["path"]: f"https://assets.example.com/{index}.png" for index, row in enumerate(execution)}

        class FakeOmni:
            submitted = []

            def __init__(self, api_key, ledger):
                pass

            def submit(self, prompt, urls, *, clip_index, aspect_ratio, reference_bindings=None, max_retries=0, retry_of=None):
                if aspect_ratio != "9:16":
                    raise AssertionError("unexpected aspect ratio")
                type(self).submitted.append(clip_index)
                if clip_index == 1:
                    raise SubmissionUnknown("ambiguous")
                return f"task-{clip_index}"

            def poll(self, task_id):
                return {"status": "completed"}

            def download_result(self, task_id, data, output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"video")
                return output

        orchestrator.omni_factory = FakeOmni
        orchestrator.key_loader = lambda service, account: "not-a-real-key"
        with self.assertRaises(SubmissionUnknown):
            orchestrator._generate_video_batches(state, public)
        self.assertEqual(sorted(FakeOmni.submitted), [1])

    def test_tts_failure_prevents_publisher_and_omni_creation(self) -> None:
        orchestrator = self._project(narration=True)
        self._register(orchestrator, 1)
        orchestrator.approve_references()
        touched = {"publisher": False, "omni": False}

        class FakeMiniMax:
            def __init__(self, api_key, ledger):
                pass

            def list_system_voices(self):
                return [{"voice_id": "Chinese (Mandarin)_Reliable_Executive", "voice_name": "沉稳高管"}]

            def synthesize(self, *args, **kwargs):
                raise ProviderError("known tts failure")

        class FakePublisher:
            def __init__(self, rows):
                touched["publisher"] = True

        class FakeOmni:
            def __init__(self, *args, **kwargs):
                touched["omni"] = True

        orchestrator.minimax_factory = FakeMiniMax
        orchestrator.publisher_cls = FakePublisher
        orchestrator.omni_factory = FakeOmni
        orchestrator.key_loader = lambda service, account: "not-a-real-key"
        with mock.patch.object(commercial_ad, "require_media_tools", return_value=("ffmpeg", "ffprobe")), mock.patch.object(
            commercial_ad.shutil, "which", return_value="/opt/homebrew/bin/cloudflared"
        ):
            with self.assertRaises(ProviderError):
                orchestrator.produce()
        self.assertFalse(touched["publisher"])
        self.assertFalse(touched["omni"])


if __name__ == "__main__":
    unittest.main()
