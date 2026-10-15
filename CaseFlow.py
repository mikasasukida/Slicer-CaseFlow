"""One-case-at-a-time NIfTI workflow for 3D Slicer 5.10+."""

import gc
import os
import time
from collections import Counter
from pathlib import Path

import qt
import slicer
import vtk
from slicer.ScriptedLoadableModule import ScriptedLoadableModule, ScriptedLoadableModuleWidget


def nifti_stem(path):
    name = Path(path).name
    if name.lower().endswith(".nii.gz"):
        return name[:-7]
    if name.lower().endswith(".nii"):
        return name[:-4]
    raise ValueError("只支持 .nii 和 .nii.gz")


def image_paths(folder, exclude=None):
    """Recursively index names only; do not read image headers or pixel data."""
    root = Path(folder)
    excluded = Path(exclude).resolve() if exclude else None
    paths = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        if excluded:
            dirs[:] = [name for name in dirs if (current_path / name).resolve() != excluded]
        for name in files:
            if name.lower().endswith((".nii", ".nii.gz")):
                paths.append(current_path / name)
    return sorted(paths, key=lambda p: str(p.relative_to(root)).casefold())


def same_geometry(first, second, tolerance=1e-3):
    a, b = first.GetImageData(), second.GetImageData()
    if not a or not b or a.GetDimensions() != b.GetDimensions():
        return False
    ma, mb = vtk.vtkMatrix4x4(), vtk.vtkMatrix4x4()
    first.GetIJKToRASMatrix(ma)
    second.GetIJKToRASMatrix(mb)
    return all(abs(ma.GetElement(i, j) - mb.GetElement(i, j)) <= tolerance
               for i in range(4) for j in range(4))


class CaseFlow(ScriptedLoadableModule):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent.title = "病例标注流程"
        self.parent.categories = ["Segmentation"]
        self.parent.dependencies = ["Segmentations"]
        self.parent.contributors = ["Local project"]
        self.parent.helpText = "文件夹只建立病例列表；每次只加载一个 NIfTI 和对应标注。"

    def setup(self):
        main_window = slicer.util.mainWindow()
        if not main_window:
            return
        self._toolbar = qt.QToolBar("快速加入文件夹", main_window)
        self._action = self._toolbar.addAction("快速加入文件夹")
        self._action.connect("triggered()", self.open_workflow)
        self._save_action = self._toolbar.addAction("保存当前 mask")
        self._save_action.connect("triggered()", self.save_current)
        self._next_action = self._toolbar.addAction("下一例")
        self._next_action.connect("triggered()", self.next_case)
        main_window.addToolBar(self._toolbar)

    def open_workflow(self):
        slicer.util.selectModule("CaseFlow")
        widget = slicer.modules.caseflow.widgetRepresentation().self()
        widget.image_path_edit.setFocus()

    def save_current(self):
        slicer.modules.caseflow.widgetRepresentation().self().save_current()

    def next_case(self):
        slicer.modules.caseflow.widgetRepresentation().self().next_case()


class CaseFlowWidget(ScriptedLoadableModuleWidget):
    def setup(self):
        super().setup()

        self.paths = []
        self.case_items = []
        self.group_items = []
        self.image_dir = None
        self.output_dir = None
        self.current_index = -1
        self.volume = None
        self.segmentation = None
        self._baseline = None
        self._loading = False

        self.layout.addWidget(qt.QLabel("快速加入 MRI 文件夹：粘贴路径后点“加入列表”"))
        self.image_path_edit = qt.QLineEdit()
        self.image_path_edit.setPlaceholderText("粘贴 MRI 总文件夹路径（可包含多级子文件夹）")
        self.image_path_edit.connect("returnPressed()", self.index_image_dir)
        self.layout.addWidget(self.image_path_edit)
        image_row = qt.QHBoxLayout()
        self.image_button = qt.QPushButton("浏览文件夹")
        self.image_button.connect("clicked()", self.browse_image_dir)
        image_row.addWidget(self.image_button)
        self.index_button = qt.QPushButton("加入列表（不加载图像）")
        self.index_button.connect("clicked()", self.index_image_dir)
        image_row.addWidget(self.index_button)
        self.layout.addLayout(image_row)
        self.image_label = qt.QLabel("尚未选择 MRI 文件夹")
        self.layout.addWidget(self.image_label)
        self.output_button = qt.QPushButton("选择 mask 输出文件夹")
        self.output_button.connect("clicked()", self.choose_output_dir)
        self.layout.addWidget(self.output_button)
        self.output_label = qt.QLabel("尚未选择输出文件夹")
        self.layout.addWidget(self.output_label)

        self.case_list = qt.QTreeWidget()
        self.case_list.setHeaderHidden(True)
        self.case_list.setUniformRowHeights(True)
        self.case_list.setMinimumHeight(160)
        self.case_list.connect("itemSelectionChanged()", self.on_item_changed)
        self.layout.addWidget(self.case_list)
        row = qt.QHBoxLayout()
        self.save_button = qt.QPushButton("保存当前 mask")
        self.save_button.connect("clicked()", self.save_current)
        row.addWidget(self.save_button)
        self.next_button = qt.QPushButton("下一例")
        self.next_button.connect("clicked()", self.next_case)
        row.addWidget(self.next_button)
        self.layout.addLayout(row)
        self.status = qt.QLabel("就绪")
        self.status.wordWrap = True
        self.layout.addWidget(self.status)

        self.layout.addWidget(qt.QLabel("选择病例后进入 Slicer 原生 Segment Editor；顶部工具栏可保存或切换下一例。"))

    def _editor(self):
        return slicer.modules.segmenteditor.widgetRepresentation().self().editor

    def _browse_directory(self, title, initial):
        dialog = qt.QFileDialog(slicer.util.mainWindow(), title, str(initial))
        dialog.setFileMode(qt.QFileDialog.Directory)
        dialog.setOption(qt.QFileDialog.ShowDirsOnly, True)
        dialog.setOption(qt.QFileDialog.DontUseNativeDialog, True)
        if not dialog.exec_():
            return None
        selected = dialog.selectedFiles()
        return str(selected[0]) if selected else None

    def browse_image_dir(self):
        folder = self._browse_directory("选择 MRI 文件夹", self.image_dir or Path.home())
        if folder:
            self.image_path_edit.text = folder
            self.index_image_dir()

    def index_image_dir(self):
        entered = str(self.image_path_edit.text).strip().strip('"')
        if not entered:
            qt.QMessageBox.warning(slicer.util.mainWindow(), "缺少文件夹", "请粘贴或浏览 MRI 文件夹路径。")
            return
        folder = Path(entered)
        if not folder.is_dir():
            qt.QMessageBox.warning(slicer.util.mainWindow(), "文件夹不存在", str(folder))
            return
        if not self._can_leave():
            return
        started = time.perf_counter()
        paths = image_paths(folder, self.output_dir)
        names = [str(p.relative_to(folder).parent / nifti_stem(p)).casefold() for p in paths]
        if len(names) != len(set(name.casefold() for name in names)):
            qt.QMessageBox.critical(slicer.util.mainWindow(), "文件名冲突", "同一子文件夹中有同名 .nii 与 .nii.gz，无法保证 mask 一一对应。")
            return
        self._release_case()
        self.image_dir, self.paths = folder, paths
        self.image_label.text = str(self.image_dir)
        self.case_list.blockSignals(True)
        self.case_list.clear()
        self.case_items = []
        self.group_items = []
        sample_dirs = [self._sample_dir(p.relative_to(folder)) for p in paths]
        sample_counts = Counter(sample_dirs)
        groups = {}
        for index, (path, sample_dir) in enumerate(zip(paths, sample_dirs)):
            if sample_dir not in groups:
                name = sample_dir.name if sample_dir != Path(".") else "根目录"
                context = str(sample_dir.parent) if sample_dir.parent != Path(".") else ""
                label = f"{name}（{sample_counts[sample_dir]} 个文件）"
                if context:
                    label += f"  ·  {context}"
                parent = qt.QTreeWidgetItem(self.case_list, [label])
                groups[sample_dir] = (parent, [])
                self.group_items.append((parent, sample_dir, groups[sample_dir][1]))
            parent, indices = groups[sample_dir]
            relative = path.relative_to(folder)
            child = qt.QTreeWidgetItem(parent, [str(relative.relative_to(sample_dir))])
            child.setData(0, qt.Qt.UserRole, index)
            self.case_items.append(child)
            indices.append(index)
        self.case_list.blockSignals(False)
        self._refresh_marks()
        self.status.text = f"已索引 {len(paths)} 例（{time.perf_counter() - started:.2f} 秒）；图像数据尚未加载。"

    def choose_output_dir(self):
        folder = self._browse_directory("选择 mask 输出文件夹", self.output_dir or Path.home())
        if folder:
            self.output_dir = Path(folder)
            self.output_label.text = str(self.output_dir)
            self._refresh_marks()

    def _mask_path(self, index):
        relative = self.paths[index].relative_to(self.image_dir)
        return self.output_dir / relative.parent / (nifti_stem(relative) + ".nii.gz")

    @staticmethod
    def _sample_dir(relative):
        return relative.parent.parent if len(relative.parts) >= 3 else relative.parent

    def _refresh_marks(self):
        for i, path in enumerate(self.paths):
            mark = "✓ " if self.output_dir and self._mask_path(i).is_file() else "○ "
            relative = path.relative_to(self.image_dir)
            self.case_items[i].setText(0, mark + str(relative.relative_to(self._sample_dir(relative))))
        for parent, sample_dir, indices in self.group_items:
            name = sample_dir.name if sample_dir != Path(".") else "根目录"
            context = str(sample_dir.parent) if sample_dir.parent != Path(".") else ""
            saved = sum(self.output_dir is not None and self._mask_path(i).is_file() for i in indices)
            label = f"{name}（{len(indices)} 个文件，{saved} 个已标注）"
            if context:
                label += f"  ·  {context}"
            parent.setText(0, label)

    def on_item_changed(self):
        item = self.case_list.currentItem()
        if item is None or item.parent() is None:
            return
        row = int(item.data(0, qt.Qt.UserRole))
        if row == self.current_index:
            return
        if not self._can_leave():
            self.case_list.blockSignals(True)
            if self.current_index >= 0:
                self.case_list.setCurrentItem(self.case_items[self.current_index])
            self.case_list.blockSignals(False)
            return
        self._release_case()
        try:
            self._load_case(row)
            if slicer.util.mainWindow():
                slicer.util.selectModule("SegmentEditor")
        except Exception as exc:
            self._release_case()
            self.case_list.blockSignals(True)
            self.case_list.clearSelection()
            self.case_list.blockSignals(False)
            qt.QMessageBox.critical(slicer.util.mainWindow(), "病例加载失败", str(exc))

    def _load_case(self, row):
        self._loading = True
        try:
            path = self.paths[row]
            self.volume = slicer.util.loadVolume(str(path))
            if not self.volume:
                raise RuntimeError(f"无法打开 MRI：{path}")
            self.segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", f"{nifti_stem(path)} mask")
            self.segmentation.CreateDefaultDisplayNodes()
            self.segmentation.SetReferenceImageGeometryParameterFromVolumeNode(self.volume)
            if self.output_dir and self._mask_path(row).is_file():
                self._import_mask(self._mask_path(row))
            else:
                self.segmentation.GetSegmentation().AddEmptySegment("liver", "肝脏", [1.0, 0.25, 0.15])
            editor = self._editor()
            editor.setSegmentationNode(self.segmentation)
            editor.setSourceVolumeNodeID(self.volume.GetID())
            if slicer.app.layoutManager():
                slicer.util.setSliceViewerLayers(background=self.volume)
                slicer.util.resetSliceViews()
            self.current_index = row
            self._baseline = self._change_stamp()
            self.status.text = f"已加载 {row + 1}/{len(self.paths)}：{path.relative_to(self.image_dir)}。"
        finally:
            self._loading = False

    def _import_mask(self, path):
        label = slicer.util.loadLabelVolume(str(path))
        if not label:
            raise RuntimeError(f"无法打开已有 mask：{path}")
        try:
            if not same_geometry(self.volume, label):
                raise ValueError(f"已有 mask 的尺寸或空间位置与 MRI 不符：{path}")
            lo, hi = label.GetImageData().GetScalarRange()
            if lo < 0 or hi > 1:
                raise ValueError(f"已有 mask 标签超出 0/1：{path}")
            ok = slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(label, self.segmentation)
            if not ok:
                raise RuntimeError(f"无法导入已有 mask：{path}")
            if self.segmentation.GetSegmentation().GetNumberOfSegments() != 1:
                raise ValueError(f"已有 mask 需要恰好一个肝脏标签：{path}")
        finally:
            self._remove_node_with_helpers(label)

    def _change_stamp(self):
        if not self.segmentation:
            return None
        data = self.segmentation.GetSegmentation()
        stamp = [data.GetMTime()]
        for index in range(data.GetNumberOfSegments()):
            segment = data.GetNthSegment(index)
            stamp.append(segment.GetMTime())
            image = segment.GetRepresentation("Binary labelmap")
            stamp.append(image.GetMTime() if image else None)
        return tuple(stamp)

    def _has_changes(self):
        return self._baseline is not None and self._change_stamp() != self._baseline

    def _can_leave(self):
        if not self._has_changes():
            return True
        answer = qt.QMessageBox.question(slicer.util.mainWindow(), "未保存标注",
            "当前标注已修改。保存后切换？选择“否”将保留在当前病例。",
            qt.QMessageBox.Yes | qt.QMessageBox.No, qt.QMessageBox.Yes)
        return answer == qt.QMessageBox.Yes and self.save_current()

    def save_current(self):
        if self.current_index < 0 or not self.segmentation:
            return False
        if not self.output_dir:
            qt.QMessageBox.warning(slicer.util.mainWindow(), "需要输出文件夹", "请先选择 mask 输出文件夹。")
            return False
        destination = self._mask_path(self.current_index)
        if destination.resolve() == self.paths[self.current_index].resolve():
            qt.QMessageBox.critical(slicer.util.mainWindow(), "路径冲突", "输出路径与 MRI 原图相同，已取消保存。")
            return False
        temp = destination.with_name(destination.stem + ".caseflow-temp.nii.gz")
        label = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "CaseFlow export")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            ok = slicer.modules.segmentations.logic().ExportVisibleSegmentsToLabelmapNode(
                self.segmentation, label, self.volume)
            if not ok or not same_geometry(self.volume, label):
                raise RuntimeError("导出 mask 后尺寸或空间位置与 MRI 不符")
            if not slicer.util.saveNode(label, str(temp)):
                raise RuntimeError("mask 文件写入失败")
            os.replace(temp, destination)
            self._baseline = self._change_stamp()
            self._refresh_marks()
            self.status.text = f"已保存：{destination}"
            return True
        except Exception as exc:
            qt.QMessageBox.critical(slicer.util.mainWindow(), "保存失败", str(exc))
            return False
        finally:
            self._remove_node_with_helpers(label)
            if temp.exists():
                temp.unlink()

    def next_case(self):
        if self.current_index + 1 < len(self.paths):
            next_item = self.case_items[self.current_index + 1]
            next_item.parent().setExpanded(True)
            self.case_list.setCurrentItem(next_item)

    def _remove_node_with_helpers(self, node):
        if not node:
            return
        helpers = [node.GetStorageNode(), node.GetDisplayNode()]
        slicer.mrmlScene.RemoveNode(node)
        for helper in helpers:
            if helper and helper.GetScene() == slicer.mrmlScene:
                slicer.mrmlScene.RemoveNode(helper)

    def _release_case(self):
        if not hasattr(self, "current_index"):
            return
        editor = self._editor()
        editor.setActiveEffect(None)
        editor.setSegmentationNode(None)
        editor.setSourceVolumeNodeID(None)
        if slicer.app.layoutManager():
            slicer.util.setSliceViewerLayers(background=None, foreground=None, label=None)
        self._remove_node_with_helpers(self.segmentation)
        self._remove_node_with_helpers(self.volume)
        self.segmentation = None
        self.volume = None
        self.current_index = -1
        self._baseline = None
        gc.collect()
