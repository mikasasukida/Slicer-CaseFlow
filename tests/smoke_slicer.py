"""Run with Slicer --python-script; writes one result file beside this script."""

import tempfile
import traceback
from pathlib import Path

import numpy as np
import qt
import slicer


RESULT = Path(__file__).with_name("smoke-result.txt")


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
            assert len(widget.paths) == 3
            assert [str(path.relative_to(images)).replace("\\", "/") for path in widget.paths] == list(case_names)
            assert widget.case_list.topLevelItemCount == 2
            assert widget.case_list.topLevelItem(1).text(0).startswith("sampleB（2 个文件")
            assert widget.case_list.topLevelItem(1).childCount() == 2
            assert widget.case_items[1].parent() == widget.case_list.topLevelItem(1)
            assert widget._mask_path(0) == masks / "sampleA/1/scan.nii.gz"
            assert widget._mask_path(1) == masks / "sampleB/1/scan.nii.gz"
            assert not slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
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
            assert widget.save_current()
            RESULT.write_text("SAVED", encoding="utf-8")
            saved = slicer.util.loadLabelVolume(str(masks / "sampleA/1/scan.nii.gz"))
            assert same_geometry(widget.volume, saved)
            assert np.array_equal(slicer.util.arrayFromVolume(saved), voxels)
            widget._remove_node_with_helpers(saved)
            widget.next_case()
            assert widget.current_index == 1
            assert len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")) == 1
            widget._release_case()
            widget._load_case(0)
            assert widget.segmentation.GetSegmentation().GetNumberOfSegments() == 1
            loaded_id = widget.segmentation.GetSegmentation().GetNthSegmentID(0)
            assert np.array_equal(
                slicer.util.arrayFromSegmentBinaryLabelmap(widget.segmentation, loaded_id, widget.volume), voxels
            )
            widget._release_case()
        RESULT.write_text("PASS: indexed nested duplicate filenames, saved mirrored mask, reloaded geometry and voxels, released case", encoding="utf-8")
        slicer.app.exit(0)
    except Exception:
        RESULT.write_text(traceback.format_exc(), encoding="utf-8")
        slicer.app.exit(1)


qt.QTimer.singleShot(0, run)
