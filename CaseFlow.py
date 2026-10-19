"""One-case-at-a-time NIfTI workflow for 3D Slicer 5.10+."""

import gc
import os
import queue
import threading
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


class ScanCancelled(Exception):
    """A superseded folder scan may stop without changing the visible list."""


def image_paths(folder, exclude=None, cancel=None, progress=None):
    """Recursively index names only; do not read image headers or pixel data."""
    root = Path(folder)
    if not root.is_dir():
        raise FileNotFoundError(f"文件夹不存在：{root}")
    excluded = Path(exclude).resolve() if exclude else None
    paths = []
    folders = 0
    def fail(error):
        raise error
    for current, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        if cancel is not None and cancel.is_set():
            raise ScanCancelled()
        folders += 1
        current_path = Path(current)
        if excluded:
            dirs[:] = [name for name in dirs if (current_path / name).resolve() != excluded]
        for name in files:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled()
            if name.lower().endswith((".nii", ".nii.gz")):
                paths.append(current_path / name)
        if progress:
            progress("scan", len(paths), folders)
    if cancel is not None and cancel.is_set():
        raise ScanCancelled()
    paths.sort(key=lambda p: str(p.relative_to(root)).casefold())
    return paths


def _sample_dir(relative):
    return relative.parent.parent if len(relative.parts) >= 3 else relative.parent


def _scan_index(folder, output_dir, cancel, messages, generation):
    """Worker thread: only filesystem and Python operations; never touch Qt or MRML."""
    started = time.perf_counter()
    last_report = 0.0

    def report(stage, count, folders):
        nonlocal last_report
        now = time.perf_counter()
        if now - last_report >= 0.2:
            messages.put((generation, "progress", stage, count, folders))
            last_report = now

    try:
        paths = image_paths(folder, output_dir, cancel, report)
        counts = Counter()
        marked = []
        previous_key = None
        for index, path in enumerate(paths):
            if cancel.is_set():
                raise ScanCancelled()
            relative = path.relative_to(folder)
            key = str(relative.parent / nifti_stem(path)).casefold()
            # The sorted NIfTI paths put .nii and .nii.gz variants of the
            # same case next to each other, so one key is enough to detect
            # collisions without retaining a second per-file set.
            if key == previous_key:
                raise ValueError("同一子文件夹中有同名 .nii 与 .nii.gz，无法保证 mask 一一对应。")
            previous_key = key
            sample = _sample_dir(relative)
            counts[sample] += 1
            if output_dir:
                mask = output_dir / relative.parent / (nifti_stem(relative) + ".nii.gz")
                marked.append(mask.is_file())
            else:
                marked.append(False)
            report("prepare", index + 1, len(paths))
        messages.put((generation, "done", paths, counts, marked, time.perf_counter() - started))
    except ScanCancelled:
        pass
    except Exception as exc:
        messages.put((generation, "error", str(exc)))


def _scan_marks(paths, image_dir, output_dir, cancel, messages, generation):
    """Worker thread for filesystem checks after selecting an output folder."""
    try:
        flags = []
        for path in paths:
            if cancel.is_set():
                raise ScanCancelled()
            relative = path.relative_to(image_dir)
            mask = output_dir / relative.parent / (nifti_stem(relative) + ".nii.gz")
            flags.append(mask.is_file())
        messages.put((generation, "done", flags))
    except ScanCancelled:
        pass
    except Exception as exc:
        messages.put((generation, "error", str(exc)))


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
        self._index_generation = 0
        self._index_cancel = None
        self._index_queue = None
        self._index_stage = None
        self._index_build = None
        self._index_started = None
        self._index_folder = None
        self._index_mark_version = 0
        self._mark_version = 0
        self._index_timer = qt.QTimer()
        self._index_timer.setInterval(30)
        self._index_timer.connect("timeout()", self._poll_index)
        self._mark_generation = 0
        self._mark_cancel = None
        self._mark_queue = None
        self._mark_flags = []
        self._mark_apply = None
        self._mark_timer = qt.QTimer()
        self._mark_timer.setInterval(30)
        self._mark_timer.connect("timeout()", self._poll_marks)

        self.layout.addWidget(qt.QLabel("快速加入 MRI 文件夹：粘贴路径后点“加入列表”"))
        self.image_path_edit = qt.QLineEdit()
        self.image_path_edit.setPlaceholderText("粘贴 MRI 总文件夹路径（可包含多级子文件夹）")
        last_image_dir = slicer.app.settings().value("CaseFlow/lastImageDir")
        if last_image_dir:
            self.image_path_edit.text = str(last_image_dir)
        self.image_path_edit.connect("returnPressed()", self.index_image_dir)
        self.layout.addWidget(self.image_path_edit)
        image_row = qt.QHBoxLayout()
        self.image_button = qt.QPushButton("浏览文件夹")
        self.image_button.connect("clicked()", self.browse_image_dir)
        image_row.addWidget(self.image_button)
        self.index_button = qt.QPushButton("加入列表（不加载图像）")
        self.index_button.connect("clicked()", self.index_image_dir)
        image_row.addWidget(self.index_button)
        self.cancel_index_button = qt.QPushButton("取消索引")
        self.cancel_index_button.connect("clicked()", self.cancel_index)
        self.cancel_index_button.setEnabled(False)
        image_row.addWidget(self.cancel_index_button)
        self.layout.addLayout(image_row)
        self.index_progress = qt.QLabel("")
        self.layout.addWidget(self.index_progress)
        self.image_label = qt.QLabel("尚未选择 MRI 文件夹")
        self.layout.addWidget(self.image_label)
        self.output_button = qt.QPushButton("选择 mask 输出文件夹")
        self.output_button.connect("clicked()", self.choose_output_dir)
        self.layout.addWidget(self.output_button)
        self.output_label = qt.QLabel("尚未选择输出文件夹")
        self.layout.addWidget(self.output_label)
        last_output_dir = slicer.app.settings().value("CaseFlow/lastOutputDir")
        if last_output_dir:
            self.output_dir = Path(str(last_output_dir))
            self.output_label.text = str(self.output_dir)

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
        entered = str(self.image_path_edit.text).strip().strip('"')
        initial = self.image_dir or (Path(entered) if entered else Path.home())
        folder = self._browse_directory("选择 MRI 文件夹", initial)
        if folder:
            self.image_path_edit.text = folder
            self.index_image_dir()

    def index_image_dir(self):
        entered = str(self.image_path_edit.text).strip().strip('"')
        if not entered:
            qt.QMessageBox.warning(slicer.util.mainWindow(), "缺少文件夹", "请粘贴或浏览 MRI 文件夹路径。")
            return
        self.cancel_index(quiet=True)
        self._cancel_marks()
        self._index_generation += 1
        generation = self._index_generation
        self._index_cancel = threading.Event()
        self._index_queue = queue.Queue()
        self._index_stage = "scanning"
        self._index_started = time.perf_counter()
        self._index_mark_version = self._mark_version
        folder = Path(entered).expanduser()
        self._index_folder = folder
        self.index_progress.text = "正在扫描文件名；当前病例列表仍可使用。"
        self.cancel_index_button.setEnabled(True)
        self._index_timer.start()
        worker = threading.Thread(target=_scan_index,
            args=(folder, self.output_dir, self._index_cancel, self._index_queue, generation),
            daemon=True)
        worker.start()

    def cancel_index(self, quiet=False):
        if self._index_cancel is not None:
            self._index_cancel.set()
        was_active = self._index_stage is not None
        self._index_generation += 1
        self._index_stage = None
        if self._index_build is not None:
            # QTreeWidget owns all staged items, including after a partial build.
            # Clearing it before releasing Python references avoids leaving
            # detached QTreeWidgetItem instances with ambiguous ownership.
            self._index_build["staging"].clear()
        self._index_build = None
        self._index_queue = None
        self._index_cancel = None
        self._index_folder = None
        self._index_timer.stop()
        self.cancel_index_button.setEnabled(False)
        if was_active and not quiet:
            self.index_progress.text = "已取消；原病例列表保持不变。"

    def _poll_index(self):
        if self._index_stage == "scanning":
            for _ in range(64):
                try:
                    message = self._index_queue.get_nowait()
                except queue.Empty:
                    break
                if message[0] != self._index_generation:
                    continue
                if message[1] == "progress":
                    _, _, stage, count, folders = message
                    if stage == "scan":
                        self.index_progress.text = f"正在扫描：已发现 {count} 个 NIfTI，经过 {folders} 个文件夹。"
                    else:
                        self.index_progress.text = f"正在核对文件名和 mask：{count}/{folders}。"
                elif message[1] == "error":
                    self.cancel_index(quiet=True)
                    self.index_progress.text = f"索引失败：{message[2]}；原病例列表保持不变。"
                    return
                elif message[1] == "done":
                    self._begin_index_build(*message[2:])
                    break
        if self._index_stage == "building":
            self._build_index_chunk()

    def _begin_index_build(self, paths, counts, marked, scan_seconds):
        self._index_stage = "building"
        self._index_build = {
            "folder": self._index_folder,
            "staging": qt.QTreeWidget(),
            "paths": paths,
            "counts": counts,
            "marked": marked,
            "roots": [],
            "groups": {},
            "group_items": [],
            "case_items": [],
            "cursor": 0,
            "scan_seconds": scan_seconds,
        }
        self.index_progress.text = f"扫描完成：{len(paths)} 个 NIfTI；正在建立列表。"

    def _build_index_chunk(self):
        state = self._index_build
        paths = state["paths"]
        end = min(state["cursor"] + 200, len(paths))
        for index in range(state["cursor"], end):
            path = paths[index]
            sample_dir = _sample_dir(path.relative_to(state["folder"]))
            if sample_dir not in state["groups"]:
                parent = qt.QTreeWidgetItem(state["staging"], [""])
                state["groups"][sample_dir] = (parent, [])
                state["roots"].append(parent)
                state["group_items"].append((parent, sample_dir, state["groups"][sample_dir][1]))
            parent, indices = state["groups"][sample_dir]
            relative = path.relative_to(state["folder"])
            mark = "✓ " if state["marked"][index] else "○ "
            child = qt.QTreeWidgetItem(parent, [mark + str(relative.relative_to(sample_dir))])
            child.setData(0, qt.Qt.UserRole, index)
            state["case_items"].append(child)
            indices.append(index)
        state["cursor"] = end
        self.index_progress.text = f"正在建立病例列表：{end}/{len(paths)}。"
        if end < len(paths):
            return
        for parent, sample_dir, indices in state["group_items"]:
            name = sample_dir.name if sample_dir != Path(".") else "根目录"
            context = str(sample_dir.parent) if sample_dir.parent != Path(".") else ""
            saved = sum(state["marked"][i] for i in indices)
            label = f"{name}（{state['counts'][sample_dir]} 个文件，{saved} 个已标注）"
            if context:
                label += f"  ·  {context}"
            parent.setText(0, label)
        self._commit_index_build()

    def _commit_index_build(self):
        state = self._index_build
        if not self._can_leave():
            self.cancel_index(quiet=True)
            self.index_progress.text = "未切换文件夹；原病例列表保持不变。"
            return
        self._cancel_marks()
        self._release_case()
        self.case_list.blockSignals(True)
        try:
            self.case_list.clear()
            while state["staging"].topLevelItemCount:
                root = state["staging"].takeTopLevelItem(0)
                self.case_list.addTopLevelItem(root)
            self.image_dir = state["folder"]
            self.paths = state["paths"]
            self.case_items = state["case_items"]
            self.group_items = state["group_items"]
            self._mark_flags = state["marked"]
            self.image_label.text = str(self.image_dir)
            slicer.app.settings().setValue("CaseFlow/lastImageDir", str(self.image_dir))
        finally:
            self.case_list.blockSignals(False)
        elapsed = time.perf_counter() - self._index_started
        marks_changed_during_scan = self._mark_version != self._index_mark_version
        self.cancel_index(quiet=True)
        self.index_progress.text = f"已索引 {len(self.paths)} 例（{elapsed:.2f} 秒）；图像数据尚未加载。"
        if marks_changed_during_scan:
            self._refresh_marks()

    def choose_output_dir(self):
        folder = self._browse_directory("选择 mask 输出文件夹", self.output_dir or Path.home())
        if folder:
            self.output_dir = Path(folder)
            self.output_label.text = str(self.output_dir)
            slicer.app.settings().setValue("CaseFlow/lastOutputDir", str(self.output_dir))
            self._mark_version += 1
            if self._index_stage is not None:
                self.index_image_dir()
            self._refresh_marks()

    def _mask_path(self, index):
        relative = self.paths[index].relative_to(self.image_dir)
        return self.output_dir / relative.parent / (nifti_stem(relative) + ".nii.gz")

    @staticmethod
    def _sample_dir(relative):
        return _sample_dir(relative)

    def _refresh_marks(self):
        self._cancel_marks()
        if not self.paths:
            return
        if not self.output_dir:
            self._mark_apply = {"flags": [False] * len(self.paths), "cursor": 0}
            self._mark_timer.start()
            return
        self._mark_generation += 1
        generation = self._mark_generation
        self._mark_cancel = threading.Event()
        self._mark_queue = queue.Queue()
        self._mark_timer.start()
        worker = threading.Thread(target=_scan_marks,
            args=(list(self.paths), self.image_dir, self.output_dir,
                  self._mark_cancel, self._mark_queue, generation), daemon=True)
        worker.start()

    def _cancel_marks(self):
        if self._mark_cancel is not None:
            self._mark_cancel.set()
        self._mark_generation += 1
        self._mark_cancel = None
        self._mark_queue = None
        self._mark_apply = None
        self._mark_timer.stop()

    def _poll_marks(self):
        if self._mark_apply is None and self._mark_queue is not None:
            try:
                message = self._mark_queue.get_nowait()
            except queue.Empty:
                return
            if message[0] != self._mark_generation:
                return
            if message[1] == "error":
                self._cancel_marks()
                self.status.text = f"mask 状态核对失败：{message[2]}"
                return
            self._mark_apply = {"flags": message[2], "cursor": 0}
            self._mark_queue = None
        if self._mark_apply is None:
            return
        state = self._mark_apply
        end = min(state["cursor"] + 200, len(self.paths))
        for index in range(state["cursor"], end):
            self._set_mark(index, state["flags"][index], update_group=False)
        state["cursor"] = end
        if end < len(self.paths):
            return
        for parent, sample_dir, indices in self.group_items:
            self._update_group_label(parent, sample_dir, indices)
        self._mark_apply = None
        self._mark_timer.stop()

    def _set_mark(self, index, marked, update_group=True):
        self._mark_flags[index] = marked
        relative = self.paths[index].relative_to(self.image_dir)
        mark = "✓ " if marked else "○ "
        self.case_items[index].setText(0, mark + str(relative.relative_to(self._sample_dir(relative))))
        if update_group:
            for parent, sample_dir, indices in self.group_items:
                if index in indices:
                    self._update_group_label(parent, sample_dir, indices)
                    break

    def _update_group_label(self, parent, sample_dir, indices):
        name = sample_dir.name if sample_dir != Path(".") else "根目录"
        context = str(sample_dir.parent) if sample_dir.parent != Path(".") else ""
        saved = sum(self._mark_flags[i] for i in indices)
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
            # A NIfTI is a complete volume; do not look for neighboring files as a series.
            # The selected volume is shown explicitly after the segmentation is ready.
            self.volume = slicer.util.loadVolume(str(path), {"singleFile": True, "show": False})
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
        label = slicer.util.loadLabelVolume(str(path), {"singleFile": True, "show": False})
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
            self._mark_version += 1
            self._set_mark(self.current_index, True)
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
        if self.volume is None and self.segmentation is None:
            return
        editor = self._editor()
        if self.segmentation and editor.segmentationNode() == self.segmentation:
            editor.setActiveEffect(None)
            # Clear snapshots before detaching the case. Restore the user's Undo setting.
            undo_enabled = bool(editor.undoEnabled)
            editor.setUndoEnabled(True)
            editor.setUndoEnabled(undo_enabled)
            # Source-volume detachment requires a valid segmentation in Slicer 5.10.
            editor.setSourceVolumeNodeID(None)
            editor.setSegmentationNode(None)
        self._clear_case_from_slice_views()
        self._remove_node_with_helpers(self.segmentation)
        self._remove_node_with_helpers(self.volume)
        self.segmentation = None
        self.volume = None
        self.current_index = -1
        self._baseline = None
        gc.collect()

    def _clear_case_from_slice_views(self):
        if not self.volume or not slicer.app.layoutManager():
            return
        volume_id = self.volume.GetID()
        image_data = self.volume.GetImageData()
        application_logic = slicer.app.applicationLogic()
        for name in slicer.app.layoutManager().sliceViewNames():
            logic = application_logic.GetSliceLogicByLayoutName(name)
            if not logic:
                continue
            composite = logic.GetSliceCompositeNode()
            if composite:
                for get_id, set_id in ((composite.GetBackgroundVolumeID, composite.SetBackgroundVolumeID),
                                       (composite.GetForegroundVolumeID, composite.SetForegroundVolumeID),
                                       (composite.GetLabelVolumeID, composite.SetLabelVolumeID)):
                    if get_id() == volume_id:
                        set_id(None)
            # Slicer 5.10 keeps the old vtkImageData on an empty reslice input.
            # Disconnect only the CaseFlow image; other displayed volumes are untouched.
            for layer in (logic.GetBackgroundLayer(), logic.GetForegroundLayer(), logic.GetLabelLayer()):
                if not layer or layer.GetVolumeNode() is not None:
                    continue
                for reslice in (layer.GetReslice(), layer.GetResliceUVW()):
                    if reslice and reslice.GetInput() == image_data:
                        reslice.SetInputData(None)
