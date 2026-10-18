# 3D Slicer 上游源码草案

[`slicer-v5.10-add-data-batch.patch`](slicer-v5.10-add-data-batch.patch) 针对 3D Slicer `v5.10.0` 的 `qSlicerDataDialog`：

- 用路径集合代替逐次搜索已加入的表格行；
- 新行初始化读取器选项时直接传入行号，避免每次从第一行查找控件；
- 批量遍历目录时将列宽调整推迟到目录完成后。

该补丁是**尚未编译和回归验证的源码草案**，没有应用到本机 Slicer 可执行文件。补丁针对 `v5.10.0`，使用零上下文差异格式；在对应标签的本地源码树上通过 `git apply --unidiff-zero --reverse --check` 和 `git diff --check`。应用到干净的 `v5.10.0` 源码时也需要给 `git apply` 加 `--unidiff-zero`。后续需要完整构建，并重点测试重复路径、排序后切换读取器、ZIP、DICOM/多文件序列、自定义读取器选项、拖放和原生“添加数据”的确认/取消操作。
