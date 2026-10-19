"""Run with Slicer --python-script; writes one result file beside this script."""

import tempfile
import time
import traceback
from pathlib import Path

import numpy as np
import qt
import slicer


RESULT = Path(__file__).with_name("smoke-result.txt")


def wait_for(condition, label, timeout=20):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        slicer.app.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"等待超时：{label}")


def wait_for_index(widget):
    wait_for(lambda: widget._index_stage is None, "后台索引完成")
    assert "索引失败" not in str(widget.index_progress.text), widget.index_progress.text


def run():
    try:
        RESULT.write_text("START", encoding="utf-8")
        widget = slicer.modules.caseflow.widgetRepresentation().self()
        RESULT.write_text("WIDGET", encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="caseflow-") as directory:
            root = Path(directory)
            images, masks = root / "images", root / "masks"
            images.mkdir()
            masks.mkdir()
            case_names = ("sampleA/1/scan.nii.gz", "sampleB/1/scan.nii.gz", "sampleB/3/scan.nii.gz")
            for i, relative in enumerate(case_names):
                destination = images / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                node = slicer.util.addVolumeFromArray(np.full((4, 6, 8), i + 1, dtype=np.int16))
                assert slicer.util.saveNode(node, str(destination))
                slicer.mrmlScene.RemoveNode(node)
            from CaseFlow import image_paths, same_geometry
            widget.output_dir = masks
            widget.image_path_edit.text = str(images)
            widget.index_image_dir()
            wait_for_index(widget)
            assert len(widget.paths) == 3
            assert [str(path.relative_to(images)).replace("\\", "/") for path in widget.paths] == list(case_names)
            assert widget.case_list.topLevelItemCount == 2
            assert widget.case_list.topLevelItem(1).text(0).startswith("sampleB（2 个文件")
            assert widget.case_list.topLevelItem(1).childCount() == 2
            assert widget.case_items[1].parent() == widget.case_list.topLevelItem(1)
            assert widget._mask_path(0) == masks / "sampleA/1/scan.nii.gz"
            assert widget._mask_path(1) == masks / "sampleB/1/scan.nii.gz"
            assert not slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
            collision = root / "collision"
            collision.mkdir()
            (collision / "duplicate.nii").touch()
            (collision / "duplicate.nii.gz").touch()
            widget.image_path_edit.text = str(collision)
            widget.index_image_dir()
            wait_for(lambda: widget._index_stage is None, "冲突文件检查完成")
            assert "索引失败" in str(widget.index_progress.text), "同名 NIfTI 扩展名冲突应被拦截"
            assert len(widget.paths) == 3, "索引失败后旧列表应保持不变"
            large = root / "large"
            for sample in range(60):
                folder = large / f"w{sample:03d}" / "cor"
                folder.mkdir(parents=True)
                for index in range(20):
                    (folder / f"scan{index:02d}.nii.gz").touch()
            widget.image_path_edit.text = str(large)
            widget.index_image_dir()
            wait_for(lambda: widget._index_stage == "building", "大列表分批建树")
            widget.cancel_index()
            assert len(widget.paths) == 3, "取消索引后旧列表应继续可用"
            assert widget.case_list.topLevelItemCount == 2
            widget.index_image_dir()
            wait_for_index(widget)
            assert len(widget.paths) == 1200
            assert widget.case_list.topLevelItemCount == 60
            assert len(widget.case_items) == 1200
            assert not slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"), "索引不应加载 NIfTI 图像"
            widget.image_path_edit.text = str(images)
            widget.index_image_dir()
            wait_for_index(widget)
            assert len(widget.paths) == 3, "旧任务不能覆盖新文件夹"
            widget.case_list.topLevelItem(0).setExpanded(True)
            widget.case_list.setCurrentItem(widget.case_items[0])
            RESULT.write_text("FIRST_LOAD", encoding="utf-8")
            assert widget.current_index == 0
            assert len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")) == 1
            segment_id = widget.segmentation.GetSegmentation().GetNthSegmentID(0)
            voxels = np.zeros((4, 6, 8), dtype=np.uint8)
            voxels[1, 2, 3] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(voxels, widget.segmentation, segment_id, widget.volume)
            RESULT.write_text("EDITED", encoding="utf-8")
            assert widget._has_changes(), "标注修改没有触发未保存提示"
            widget._refresh_marks()
            assert widget.save_current()
            wait_for(lambda: not widget._mark_timer.isActive(), "保存后的 mask 状态重新核对")
            assert widget.case_items[0].text(0).startswith("✓ ")
            RESULT.write_text("SAVED", encoding="utf-8")
            saved = slicer.util.loadLabelVolume(str(masks / "sampleA/1/scan.nii.gz"))
            assert same_geometry(widget.volume, saved)
            assert np.array_equal(slicer.util.arrayFromVolume(saved), voxels)
            widget._remove_node_with_helpers(saved)
            widget.next_case()
            assert widget.current_index == 1
            assert len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")) == 1
            editor = widget._editor()
            previous_image = widget.volume.GetImageData()
            undo_enabled = bool(editor.undoEnabled)
            widget._release_case()
            assert editor.segmentationNode() is None
            assert editor.sourceVolumeNode() is None
            assert bool(editor.undoEnabled) == undo_enabled
            for name in slicer.app.layoutManager().sliceViewNames():
                slice_logic = slicer.app.applicationLogic().GetSliceLogicByLayoutName(name)
                for layer in (slice_logic.GetBackgroundLayer(), slice_logic.GetForegroundLayer(), slice_logic.GetLabelLayer()):
                    if layer:
                        assert layer.GetReslice().GetInput() != previous_image
                        assert layer.GetResliceUVW().GetInput() != previous_image
            previous_image = None
            widget._load_case(0)
            assert widget.segmentation.GetSegmentation().GetNumberOfSegments() == 1
            loaded_id = widget.segmentation.GetSegmentation().GetNthSegmentID(0)
            assert np.array_equal(
                slicer.util.arrayFromSegmentBinaryLabelmap(widget.segmentation, loaded_id, widget.volume), voxels
            )
            widget._release_case()
            other = slicer.util.addVolumeFromArray(np.zeros((4, 6, 8), dtype=np.int16))
            widget._load_case(1)
            slicer.util.setSliceViewerLayers(background=other)
            widget._release_case()
            for name in slicer.app.layoutManager().sliceViewNames():
                slice_logic = slicer.app.applicationLogic().GetSliceLogicByLayoutName(name)
                assert slice_logic.GetSliceCompositeNode().GetBackgroundVolumeID() == other.GetID()
            widget._remove_node_with_helpers(other)
            assert not slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
        RESULT.write_text("PASS: async index, nested groups, mask geometry and voxels, case cleanup, unrelated volume preserved", encoding="utf-8")
        slicer.app.exit(0)
    except Exception:
        RESULT.write_text(traceback.format_exc(), encoding="utf-8")
        slicer.app.exit(1)


qt.QTimer.singleShot(0, run)
