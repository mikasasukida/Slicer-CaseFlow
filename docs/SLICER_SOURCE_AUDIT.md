# 3D Slicer 5.10.0 数据导入与标注性能审查

这份审查以用户实际安装的 3D Slicer 5.10.0 为版本基准，追踪“选文件夹 → 加入列表 → 加载图像 → 勾画 → 切换病例”的源码路径。目标是让大批量病例的操作更快、内存占用更稳定，同时保留 Slicer 原有功能。下列“源码确认”和“实测”分别标注；尚未测量的开销只列为假设。

## 1. 两段卡顿的源码路径

### 选择文件夹

`qSlicerDataDialogPrivate::addDirectory()` 调用 `qSlicerStandardFileDialog::getExistingDirectory()`；后者创建 `ctkFileDialog` 并同步执行 `exec()`。这条路径解释了为什么用户必须等待文件夹对话框返回，**但还不足以判定文件夹浏览卡顿究竟来自 Qt/CTK、Windows Shell、磁盘还是网络路径**。需要单独计时与 UI 响应测量。

源码：[qSlicerDataDialog.cxx](https://github.com/Slicer/Slicer/blob/v5.10.0/Base/QTGUI/qSlicerDataDialog.cxx)、[qSlicerFileDialog.cxx](https://github.com/Slicer/Slicer/blob/v5.10.0/Base/QTGUI/qSlicerFileDialog.cxx)。

### 选择后加入与加载

`qSlicerDataDialogPrivate::addDirectory(const QDir&)` 同步递归遍历目录。每层先调用 `examineFileInfoList()` 检查是否有读取器识别整个目录，再逐文件执行 `addFile()`。`addFile()` 会查询该文件可用的读取器，并向 `QTableWidget` 插入一行及类型/选项控件。选中很多文件时，这些动作都在对话框执行路径上。按“确定”后，`qSlicerCoreIOManager::loadNodes(files)` 又逐文件调用读取器加载。由此可把“建列表卡顿”和“加载体数据卡顿”分开测量与优化。

源码：[数据对话框](https://github.com/Slicer/Slicer/blob/v5.10.0/Base/QTGUI/qSlicerDataDialog.cxx)、[核心 I/O 管理器](https://github.com/Slicer/Slicer/blob/v5.10.0/Base/QTCore/qSlicerCoreIOManager.cxx)。

## 2. 内存路径：确认与待测

- **源码确认**：`vtkMRMLVolumeArchetypeStorageNode::ReadDataInternal()` 调用 ITK 读取器的 `Update()`，随后把读取结果关联到 MRML 体节点。把 100 个 MRI 全部作为节点保留，至少会同时保留这些图像的解压体素数据。[体数据读取源码](https://github.com/Slicer/Slicer/blob/v5.10.0/Libs/MRML/Core/vtkMRMLVolumeArchetypeStorageNode.cxx)
- **源码确认**：切片显示通过 `vtkMRMLSliceLayerLogic` 的 `vtkImageReslice` 等管线生成显示内容。存在显示管线和输出缓存，但不能仅凭它推断“每个切片视图都复制一份完整原图”。[切片层源码](https://github.com/Slicer/Slicer/blob/v5.10.0/Libs/MRML/Logic/vtkMRMLSliceLayerLogic.cxx)
- **源码确认**：MRML 场景的撤销/重做默认关闭；Segment Editor 另有独立的分割历史，脚本模块把历史上限设为 10。是否实际保留历史，取决于编辑器的撤销设置和用户操作，不能把“10 份副本”当作每例固定开销。[MRML 文档](https://github.com/Slicer/Slicer/blob/v5.10.0/Docs/developer_guide/mrml_overview.md)、[Segment Editor 源码](https://github.com/Slicer/Slicer/blob/v5.10.0/Modules/Scripted/SegmentEditor/SegmentEditor.py)、[分割编辑逻辑](https://github.com/Slicer/Slicer/blob/v5.10.0/Modules/Loadable/Segmentations/Logic/vtkSlicerSegmentEditorLogic.cxx)
- **待测**：切换病例后，VTK/Qt 管线、Segment Editor 历史和进程内存分配器分别保留多少内存；“工作集没有马上下降”并不必然表示病例仍在场景中。

## 3. 现有 CaseFlow 已做到什么

CaseFlow 只对文件名建立索引，点击树中的具体序列才调用 Slicer 读取器。切换时解除切片显示引用、移除本例图像与标注节点；mask 保存前核对几何信息，并在输出目录镜像原图相对路径。当前本机试验：703 个样本组、2023 个 NIfTI 文件建立样本树约 0.68 秒，未加载 MRI 体数据；另一次连续打开并移除 100 例约 12.8 秒，结束时没有病例节点残留。两个结果来自不同试验，不能合并推断峰值内存。

CaseFlow 目前仍在界面线程执行 `os.walk`、建树和逐项检查 mask 文件是否存在。对本机 2023 个文件尚快，但更大的目录或较慢的存储设备可能再次让界面停顿。读取器默认还可能尝试查找同目录的序列；Slicer 5.10.0 文档提供 `singleFile` 和 `show` 选项，适合对独立 3D NIfTI 做针对性对照试验。[体数据加载选项](https://github.com/Slicer/Slicer/blob/v5.10.0/Docs/developer_guide/script_repository/volumes.md)

## 4. 改进顺序与验收条件

| 顺序 | 改动 | 验收条件 |
| --- | --- | --- |
| 1 | 给文件夹选择、索引、单例读取、切换、保存分别计时；记录 UI 心跳、场景节点数、进程私有字节和工作集 | 能复现卡顿阶段，得到改动前基线，不用真实数据写入 |
| 2 | 在 CaseFlow 中把大目录索引移出界面线程，分批建树，提供进度与取消；所有 MRML/Qt 控件操作仍留在界面线程 | 大目录扫描期间界面仍可响应；取消后不留下半成品病例列表 |
| 3 | 对独立 NIfTI 测试 `singleFile=True`、延后显示更新；检查切换时 Segment Editor 历史和显示管线的引用 | 加载与切换时间不变差，空间方向及 mask 体素不变，连续切换后内存趋于稳定 |
| 4 | 在 Slicer C++ 源码中为原生“添加数据”设计轻量目录索引/按需加载入口，保留原有通用导入行为 | DICOM、NIfTI、场景等原有读入测试通过；大目录不再阻塞 UI；仅选择加载的文件进入 MRML 场景 |
| 5 | 通过 Qt 翻译资源补齐相关界面中文，不把核心字符串直接写死在 C++ 代码里 | 中英文均可切换，原有模块正常工作 |

第 4 步涉及 Slicer C++、CTK、VTK/ITK 的完整构建与回归测试，不能用仅运行 Python 模块的结果代替。启动耗时和 3D/表面表示应先单独分析，再决定是否改；不能为了降低基线内存直接删除用户可能需要的功能。
