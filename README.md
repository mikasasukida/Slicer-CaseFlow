# Slicer CaseFlow：逐例 NIfTI 标注流程

CaseFlow 是一个 3D Slicer 脚本模块，为多级文件夹中的 NIfTI 病例增加中文样本树和逐例标注流程。它保留 Slicer 原有模块、切片工具和 3D 功能；标注仍使用原生 Segment Editor。

## 功能

- 递归索引 `.nii`、`.nii.gz` 的文件名，建列表时不读取图像体素。
- 按样本编号分组，展开样本后选择其中的序列；同名样本显示上级目录，以区分批次。
- 每次只加载选中的一例，切换时移除上一例的图像和标注节点。
- 导出背景为 0、肝脏为 1 的 NIfTI mask，并核对尺寸及空间位置。
- 在 mask 输出目录中保留原图的相对目录结构，避免不同样本的同名文件互相覆盖。
- 切换有未保存修改的标注时提示保存。

## 安装

已在 3D Slicer 5.10.0（Windows）验证。下载本仓库后，将 `CaseFlow.py` 所在目录加入 Slicer 的额外模块路径，然后重新启动 Slicer。例如：

```powershell
& 'C:\path\to\Slicer.exe' --additional-module-paths 'C:\path\to\Slicer-CaseFlow'
```

也可以将 `CaseFlow.py` 复制到 Slicer 安装目录的 `lib/Slicer-<版本>/qt-scripted-modules/` 中，之后直接启动原来的 `Slicer.exe`。升级或重装 Slicer 后，需要重新放入模块。

## 使用

1. 点击顶部“快速加入文件夹”，粘贴 MRI 总文件夹路径，点击“加入列表”。也可以在模块搜索中选择“病例标注流程”。
2. 展开样本编号，点击需要标注的序列。此时才加载该例 MRI，并进入 Segment Editor。
3. 选择独立的 mask 输出文件夹，完成勾画后点击“保存当前 mask”或“下一例”。

例如，原图是 `MRI总目录/批次/分组/W750/3/scan.nii.gz`，输出是 `mask输出目录/批次/分组/W750/3/scan.nii.gz`。输出文件夹应与原图文件夹分开，以免已有 mask 被再次索引为原图。

目前面向单标签肝脏 mask；只索引 NIfTI，不索引 DICOM。Slicer 原有“添加数据”仍可用于其他格式。

## 验证

在一组本地多级 NIfTI 目录中，索引并建立 703 个样本组、2023 个文件的树用时约 0.68 秒，期间没有加载 MRI 体数据。使用临时生成的图像验证了点击子项加载、“下一例”切换、同名文件分目录保存，以及 mask 体素和空间信息在重新载入后保持一致。

另一次逐例加载并移除 100 个真实 MRI 的试验用时约 12.8 秒，进程工作集峰值约 499 MB。以上是本机自动化试验，尚未覆盖长时间人工勾画、大量撤销或其他模块同时运行时的内存表现。

`tests/smoke_slicer.py` 使用临时合成图像，不需要真实 MRI 数据。可通过 Slicer 的 `--python-script` 参数运行。该脚本会在自身旁边写入 `smoke-result.txt`，完成后自动退出 Slicer。
