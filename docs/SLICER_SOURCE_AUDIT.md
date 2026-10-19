# 3D Slicer 5.10.0 数据导入与标注性能审查

这份审查以用户实际安装的 3D Slicer 5.10.0 为版本基准，追踪“选文件夹 → 加入列表 → 加载图像 → 勾画 → 切换病例”的源码路径。目标是让大批量病例的操作更快、内存占用更稳定，同时保留 Slicer 原有功能。下列“源码确认”和“实测”分别标注；尚未测量的开销只列为假设。

## 1. 两段卡顿的源码路径

### 选择文件夹

`qSlicerDataDialogPrivate::addDirectory()` 调用 `qSlicerStandardFileDialog::getExistingDirectory()`；后者创建 `ctkFileDialog` 并同步执行 `exec()`。这条路径解释了为什么用户必须等待文件夹对话框返回，**但还不足以判定文件夹浏览卡顿究竟来自 Qt/CTK、Windows Shell、磁盘还是网络路径**。需要单独计时与 UI 响应测量。

源码：[qSlicerDataDialog.cxx](https://github.com/Slicer/Slicer/blob/v5.10.0/Base/QTGUI/qSlicerDataDialog.cxx)、[qSlicerFileDialog.cxx](https://github.com/Slicer/Slicer/blob/v5.10.0/Base/QTGUI/qSlicerFileDialog.cxx)。

### 选择后加入与加载

`qSlicerDataDialogPrivate::addDirectory(const QDir&)` 同步递归遍历目录。每层先调用 `examineFileInfoList()` 检查是否有读取器识别整个目录，再逐文件执行 `addFile()`。`addFile()` 会查询该文件可用的读取器，并向 `QTableWidget` 插入一行及类型/选项控件。选中很多文件时，这些动作都在对话框执行路径上。按“确定”后，`qSlicerCoreIOManager::loadNodes(files)` 又逐文件调用读取器加载。由此可把“建列表卡顿”和“加载体数据卡顿”分开测量与优化。

更具体的可改进处：`addFile()` 每次用 `findItems()` 在已有行里查重；新建类型下拉框后，`setCurrentIndex(0)` 触发 `onFileTypeChanged()`，再由 `senderRow()` 从第一行逐行找回刚加入的控件；`setFileOptions()` 还在每行重算选项列宽。这些操作会随已加入行数增加，批量建表有可从源码直接确认的重复遍历。上游 C++ 修改应保留排序、重复路径、ZIP、DICOM/文件序列、自定义读取器及选项的行为。[建表与查重](https://github.com/Slicer/Slicer/blob/v5.10.0/Base/QTGUI/qSlicerDataDialog.cxx#L190-L263)、[行查找与列宽](https://github.com/Slicer/Slicer/blob/v5.10.0/Base/QTGUI/qSlicerDataDialog.cxx#L370-L447)。

源码：[数据对话框](https://github.com/Slicer/Slicer/blob/v5.10.0/Base/QTGUI/qSlicerDataDialog.cxx)、[核心 I/O 管理器](https://github.com/Slicer/Slicer/blob/v5.10.0/Base/QTCore/qSlicerCoreIOManager.cxx)。

## 2. 内存路径：确认与待测

- **源码确认**：`vtkMRMLVolumeArchetypeStorageNode::ReadDataInternal()` 调用 ITK 读取器的 `Update()`，随后把读取结果关联到 MRML 体节点。把 100 个 MRI 全部作为节点保留，至少会同时保留这些图像的解压体素数据。[体数据读取源码](https://github.com/Slicer/Slicer/blob/v5.10.0/Libs/MRML/Core/vtkMRMLVolumeArchetypeStorageNode.cxx)
- **源码确认**：切片显示通过 `vtkMRMLSliceLayerLogic` 的 `vtkImageReslice` 等管线生成显示内容。存在显示管线和输出缓存，但不能仅凭它推断“每个切片视图都复制一份完整原图”。[切片层源码](https://github.com/Slicer/Slicer/blob/v5.10.0/Libs/MRML/Logic/vtkMRMLSliceLayerLogic.cxx)
- **源码确认**：MRML 场景的撤销/重做默认关闭；Segment Editor 另有独立的分割历史，脚本模块把历史上限设为 10。是否实际保留历史，取决于编辑器的撤销设置和用户操作，不能把“10 份副本”当作每例固定开销。[MRML 文档](https://github.com/Slicer/Slicer/blob/v5.10.0/Docs/developer_guide/mrml_overview.md)、[Segment Editor 源码](https://github.com/Slicer/Slicer/blob/v5.10.0/Modules/Scripted/SegmentEditor/SegmentEditor.py)、[分割编辑逻辑](https://github.com/Slicer/Slicer/blob/v5.10.0/Modules/Loadable/Segmentations/Logic/vtkSlicerSegmentEditorLogic.cxx)
- **待测**：切换病例后，VTK/Qt 管线、Segment Editor 历史和进程内存分配器分别保留多少内存；“工作集没有马上下降”并不必然表示病例仍在场景中。

运行时探针进一步确认：在 Slicer 5.10.0 中清空切片视图并移除 MRI 节点后，Red 视图的重采样管线仍把上一例 `vtkImageData` 保留在输入端；节点已不存在并不意味着这份体素数据已无引用。CaseFlow 在移除自己的病例前额外断开指向该病例的空白切片层输入，同时保留其他图像的显示。[切片层空节点分支](https://github.com/Slicer/Slicer/blob/v5.10.0/Libs/MRML/Logic/vtkMRMLSliceLayerLogic.cxx#L625-L745)。

源码还显示 Segment Editor 的历史对象在解绑分割节点后可能继续引用上例分割。CaseFlow 可通过编辑器公开接口清空撤销状态并恢复原撤销设置，但历史对象的分割指针需要在 Slicer C++ 逻辑的空节点分支修复；此项目前仍是源码推断，未做运行时内存归因。[编辑器逻辑](https://github.com/Slicer/Slicer/blob/v5.10.0/Modules/Loadable/Segmentations/Logic/vtkSlicerSegmentEditorLogic.cxx#L2114-L2126)、[分割历史](https://github.com/Slicer/Slicer/blob/v5.10.0/Libs/vtkSegmentationCore/vtkSegmentationHistory.cxx#L65-L80)。

## 3. 现有 CaseFlow 已做到什么

CaseFlow 只对文件名建立索引，点击树中的具体序列才调用 Slicer 读取器。切换时解除切片显示引用、移除本例图像与标注节点；mask 保存前核对几何信息，并在输出目录镜像原图相对路径。早期同步索引版本在本机建立 703 个样本组、2023 个 NIfTI 文件的树约需 0.68 秒，未加载 MRI 体数据；另一次连续打开并移除 100 例约需 12.8 秒，结束时没有病例节点残留。两个结果来自不同试验，不能合并推断峰值内存。

目前 CaseFlow 已把文件名扫描、mask 是否存在的检查移到后台线程；Qt 主线程每次只建立一小批树项，支持取消，且旧列表会留到新列表完整建立后再切换。上次使用的原图与输出路径会留在本机 Slicer 设置中，减少重复浏览。对独立 3D NIfTI 使用 `singleFile=True`，延后自动显示，病例准备完毕后显式显示。[体数据加载选项](https://github.com/Slicer/Slicer/blob/v5.10.0/Docs/developer_guide/script_repository/volumes.md)

本机对同一组 100 个真实 MRI 做只读对照，每组分别启动 Slicer 并逐例打开、显示、移除；两组结束后场景中均无病例体节点。默认设置的单例“加载加显示”耗时中位数为 94 毫秒，`singleFile=True, show=False` 后显式显示为 65 毫秒。进程私有字节从各自启动基线至第 100 例结束，分别增加约 58 MB 和 32 MB；工作集分别增加约 66 MB 和 58 MB。两组启动顺序和系统缓存不同，内存差值只能作为趋势，不能直接归因于某个选项。另将前 100 例分别用两种设置载入并逐例比较，体素、尺寸、IJK→RAS 矩阵全部一致；合成病例的 mask 保存与重新载入测试也通过。上述数据来自本机特定 MRI，不能推断所有格式或机器的表现。

当前后台版本在另一组真实目录里索引 680 个 NIfTI 用时约 3.54 秒，索引调用本身约 0.61 毫秒即返回；20 毫秒界面心跳在此期间持续运行，最长间隔约 47 毫秒，且没有加载体节点。对完整 CaseFlow 流程再逐例加载和释放 100 个真实 MRI：每例结束后均无体节点和分割节点，第 100 例后工作集比本次启动基线高约 77 MB，私有字节高约 63 MB。它们没有随病例数按图像大小持续累加；但这不是长时人工编辑或所有内存分配器行为的证明。两次真实目录测试使用了不同目录和流程，耗时不能横向当作同一基准比较。

## 4. 改进顺序与验收条件

| 顺序 | 改动 | 验收条件 |
| --- | --- | --- |
| 1 | 给文件夹选择、索引、单例读取、切换、保存分别计时；记录 UI 心跳、场景节点数、进程私有字节和工作集 | 索引、单例读取与切换已有基线；原生文件选择器和长时保存仍待单独测量 |
| 2 | CaseFlow 后台扫描、分批建树与取消 | 已有 1200 文件的合成目录测试；慢速磁盘和长期手工使用仍待测 |
| 3 | 独立 NIfTI 的 `singleFile=True`、延后显示及切换清理 | 100 例体素与几何一致；100 例内存/时间对照及合成 mask 回读通过；长时勾画仍待测 |
| 4 | 在 Slicer C++ 中减少原生“添加数据”的重复遍历，并修复空切片层/空分割历史引用 | 需要构建 Slicer 并运行 DICOM、NIfTI、场景、读取器选项及交互回归测试 |
| 5 | 通过 Qt 翻译资源补齐相关界面中文，不把核心字符串直接写死在 C++ 代码里 | 中英文均可切换，原有模块正常工作 |

第 4 步涉及 Slicer C++、CTK、VTK/ITK 的完整构建与回归测试，不能用仅运行 Python 模块的结果代替。启动耗时和 3D/表面表示应先单独分析，再决定是否改；不能为了降低基线内存直接删除用户可能需要的功能。

针对原生“添加数据”重复遍历，已准备[上游 C++ 源码补丁草案](../patches/README.md)。它目前只供审阅，尚未编译，也未进入已安装的 Slicer。

### 索引临时内存改进

CaseFlow 的后台索引不再为每个 NIfTI 额外保存样本目录和重复检查键集合；路径列表在原地排序，建树时再计算样本目录。这样减少了索引期间的逐文件 Python 对象，尚未单独测出具体节省的 MB 数。1200 文件 smoke 已覆盖分组、取消后旧列表保留、重新建树及同名 `.nii`/`.nii.gz` 冲突拦截。该改动影响文件索引元数据，不改变 MRI 体素的按例加载与释放。
