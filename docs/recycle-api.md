# 迅雷回收站接口探索

> 历史验证 / 调研记录；机器编号及统计为当时测试数据。最终功能与已知限制以 [README](../README.md) 和 [云盘集成说明](cloud-integration.md) 为准。

2026-10-23 对当前账号下的 101、102、103、104 四台 Windows 机器完成只读实测。四台当前迅雷版本均为 `25.1.12.1624`，能够通过已有 SSH + 命名管道接口读取迅雷自身回收站中的任务记录。

## 查询入口

```powershell
# 第二个参数是视图类型，3 为回收站。
$reply = Invoke-ThunderApi $connection 'CategoryManagerCategoryViewGetTasks' @(-1, 3)
$ids = @($reply.data)

# 根据列表里的任务 ID 查询详情；不调用删除、恢复或清空方法。
foreach ($id in $ids) {
    $task = (Invoke-ThunderApi $connection 'TaskManagerTaskGetTaskBase' @($id)).data
    if ($null -eq $task -or $task.taskId -ne $id) { continue }
    [pscustomobject]@{
        taskId = $task.taskId
        taskName = $task.taskName
        taskStatus = $task.taskStatus
        recycleTime = $task.recycleTime
        fileSize = $task.fileSize
        downloadSize = $task.downloadSize
        savePath = $task.savePath
    }
}
```

已验证的视图参数：

| 调用 | 内容 |
|---|---|
| `CategoryManagerCategoryViewGetTasks(-1, 1)` | 未完成任务，包含下载、排队及暂停等状态 |
| `CategoryManagerCategoryViewGetTasks(-1, 2)` | 已完成任务 |
| `CategoryManagerCategoryViewGetTasks(-1, 3)` | 迅雷回收站任务 |

## 实机结果

| 机器 | 未完成任务 | 已完成任务 | 回收站任务 | 回收站详情读取成功 |
|---|---:|---:|---:|---:|
| pc-101 | 416 | 89 | 71 | 71 |
| pc-102 | 768 | 179 | 10 | 10 |
| pc-103 | 753 | 297 | 41 | 41 |
| pc-104 | 763 | 385 | 36 | 36 |

数量是查询时的快照，会随迅雷使用变化。共读取 158 条回收站记录，全部有任务名称、保存路径、大小和正数 `recycleTime`；没有丢失的任务 ID，回收站 ID 与另外两个视图无交集。未修改任何远端任务。

## 可用字段与判断方式

- `taskId`、`taskName`：任务 ID 与名称。
- `fileSize`、`downloadSize`：总大小与已下载大小，单位字节。
- `savePath`：原保存路径；返回记录不证明磁盘文件仍然存在。
- `recycleTime`：进入迅雷回收站的 Unix 毫秒时间戳，例如 `1792566875968`。
- `taskStatus`、`taskType`：原任务状态与类型。
- 另有 `createTime`、`completionTime`、`url`、`refUrl`、`cid`、`gcid` 等详情字段。

回收站中的 `taskStatus` 实测包含 `7`（暂停）和 `8`（已完成），因此不能把状态 7 当作回收站标志，也不能把状态 8 直接当作仍在已完成列表中。以视图 3 的成员关系判断回收站归属，`recycleTime > 0` 可作为本轮实测的辅助核对。

已在 `remote/thunder_ipc.ps1` 中返回时间与来源链接字段，后端使用独立归档缓存读取已完成与回收站，每 60 秒刷新，手动刷新可强制更新。看板、机器卡片及任务列表均有查看入口，详情显示原状态、进度、路径、来源链接和删除时间。归档查询沿用认证会话所属用户的机器及数据目录。

回收站提供详情，以及用户显式选择的单项／批量转存云盘入口；转存保存原链接及 BT 文件选择，不恢复原记录。2026-10-27 起，新确认的转存会在云盘整项缓存完成后清理对应回收站记录，并保留本地文件；历史请求不自动补清理。不提供暂停、删除、自动清理或大盘一键加速。后端归档记录不会进入实时速度计时器或操作候选集；执行暂停/删除前，还会在远端核对任务仍属于未完成视图，且 `recycleTime <= 0`，防止预览后任务转移造成误操作。归档读取失败保留上次结果并显示读取失败，统计数标为未知。

新增来源链接会增大远端响应；SSH 接收逻辑已改为等待 EOF/连接关闭及进程退出，避免提前收到退出状态时截断尾部 JSON。同时只按换行字符划分协议记录，避免链接或名称中的 Unicode 分隔符造成误切分；有延迟尾部大响应回归测试。

## 验证范围

本轮完成查看功能并部署到 Docker。新镜像只读复核四台机器：pc-101 回收站 71 / 已完成 93，pc-102 回收站 10 / 已完成 183，pc-103 回收站 41 / 已完成 307，pc-104 回收站 36 / 已完成 388；回收站 158 条全部返回来源链接。56 项测试通过，另通过 JavaScript 语法及模拟 DOM 的三种视图、操作按钮、看板入口和空工作空间验证；未进行真实浏览器视觉验收。此处回收站指迅雷内部任务回收站。未验证 Windows 系统回收站，未调用恢复、彻底删除或清空接口。旧的 102 实机报告记录的是迅雷 12.4.7；本次 102 已是 25.1.12，当前读取成功。

新版 CLI 云盘功能已集成，行为与验证边界见 [cloud-integration.md](cloud-integration.md)。此前接口研究保留在 [cloud-workflow-research.md](cloud-workflow-research.md)。
