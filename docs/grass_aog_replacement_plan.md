# grass 插件替换 AOG 数据源方案

## 1. 目标与边界

目标：替换 `grass_on_aog` 对 AOG 的运行时依赖，避免 `arkonegraph.herokuapp.com` 不可用或 API 结构变化导致一键长草失败。

本次范围：
- 为 `grass` 选择新的材料推荐关卡数据源
- 保留“读取库存中最少的蓝材料，然后选择推荐关卡”的现有行为
- 为后续实现提供接口适配、缓存和回退策略
- 顺带梳理 `MaterialPlanning` 中对 AOG 推荐关卡集合的依赖

本次不做：
- 重写刷图规划器的线性规划逻辑
- 改变库存识别、关卡导航和战斗执行流程
- 引入需要用户登录的外部服务

---

## 2. 现状与问题

当前相关调用点：

- `Arknights/addons/contrib/grass_on_aog/__init__.py`
  - `choose_stage()` 当前使用 `get_t3_item_map_from_yituliu()`
  - 旧接口为 `https://backend.yituliu.cn/stage/t3?expCoefficient=0.625`
  - 该接口当前不再返回可用数据
- `Arknights/addons/contrib/common_cache/__init__.py`
  - `load_aog_data()` 写死 `https://arkonegraph.herokuapp.com/total/CN`
  - `filter_latest_activity_t3_item_stage()` 仍通过 `load_aog_data()['tier']['t3']` 获取 T3 材料 ID
- `penguin_stats/MaterialPlanning.py`
  - `request_data()` 写死 `https://arkonegraph.herokuapp.com/total/CN`
  - 只使用 AOG 的推荐关卡集合过滤本地规划结果

主要问题：

- 旧 AOG Heroku 服务不可作为可靠运行时依赖
- 一图流旧 `/stage/t3` 接口已失效
- 当前代码把“推荐关卡数据源”和“AOG 数据结构”耦合在一起，后续任何外部 API 改动都会影响业务逻辑
- 活动关卡偏好逻辑部分依赖 AOG 的 T3 材料列表，替换时不能只改普通关卡推荐

---

## 3. 候选数据源

### 3.1 一图流当前矩阵镜像

当前可用地址：

```text
https://cos.yituliu.cn/arknights/stage-drop/matrix.json
```

优点：
- 由一图流后端定时发布，当前返回 HTTP 200，避免已下线的 `/stage/t3`、`/stage/result`
- 与一图流前端使用同一份企鹅物流掉落矩阵
- 项目已有 Penguin 元数据和矩阵推荐算法，可直接适配为 `itemName -> stageCode`

风险：
- 返回的是原始矩阵，不是旧版预计算推荐表，需要结合关卡和物品元数据计算
- 矩阵镜像本身不携带 `expCoefficient`，当前适配沿用粗略的 T3 期望理智选择

结论：作为一图流源首选，解析失败时回退到 Penguin。

### 3.2 企鹅物流 API

候选接口：

```text
https://penguin-stats.io/PenguinStats/api/v2/result/matrix?server=CN
https://penguin-stats.io/PenguinStats/api/v2/stages
https://penguin-stats.io/PenguinStats/api/v2/items
```

优点：
- 公开、稳定、数据底层权威
- 项目中 `arkplanner` 和掉落上报已经依赖企鹅物流数据模型
- 适合作为一图流不可用时的兜底

缺点：
- 只提供原始掉落矩阵，不直接提供“推荐关卡”
- 若要做到接近 AOG / 一图流，需要本地计算物品价值、合成收益和综合效率

结论：适合作为兜底源，不建议作为首版主推荐源，除非接受只按主产物期望理智粗略选关。

### 3.3 AOG 继续兼容

旧接口：

```text
https://arkonegraph.herokuapp.com/total/CN
```

当前问题：
- 连接不可用或超时
- 即使未来恢复，也不应继续作为单点运行时依赖

结论：只保留缓存兼容或迁移说明，不再作为默认源。

---

## 4. 推荐架构

新增一个推荐源适配层，业务代码只依赖统一结构：

```python
{
    "固源岩组": {
        "item_id": "30013",
        "stage_code": "1-7",
        "source": "yituliu",
        "efficiency": 1.23,
        "updated_at": "..."
    }
}
```

建议模块边界：

```text
Arknights/addons/contrib/material_recommendation/
  __init__.py
  yituliu.py
  penguin.py
  cache.py
```

`grass_on_aog` 只做三件事：
- 读取库存
- 找到库存最少且未排除的 T3 材料
- 从统一推荐表里取 `stage_code`

推荐源优先级：

```text
一图流矩阵镜像
  -> 本地缓存
  -> 企鹅物流粗略兜底
  -> 配置的 normal_action / no_aog_data_action
```

---

## 5. 实施步骤

### 5.1 阶段一：接口适配

- 新增一图流推荐源客户端
- 请求一图流维护的 COS 矩阵镜像
- 结合 Penguin 元数据生成并转换为 `itemName -> recommendation`
- 保留 `expCoefficient` 和 `sampleSize` 为常量或配置项
- 请求失败时抛出明确异常，不在适配层直接执行刷图决策

验收标准：
- 单元测试能用 fixture 覆盖正常数据、空数据、字段缺失和 HTTP 失败
- `grass` 能从适配后的推荐表拿到 T3 蓝材料关卡

### 5.2 阶段二：替换 grass 调用

- 用新适配层替换 `get_t3_item_map_from_yituliu()`
- 将日志中的 `aog` 描述改为“推荐源”或“一图流”
- 保留 `exclude`、`prefer_activity_stage`、`normal_action`、`no_aog_data_action` 的现有语义
- `filter_latest_activity_t3_item_stage()` 不再依赖 `load_aog_data()['tier']['t3']`，改用游戏数据或推荐表中的 T3 材料 ID

验收标准：
- `GrassAddOn.choose_stage()` 在缓存可用时不访问旧 AOG
- 活动关卡偏好逻辑仍能选择当前活动 T3 掉落关
- 外部推荐源不可用时不会直接崩溃，按配置降级

### 5.3 阶段三：处理 MaterialPlanning

- 将 `aog_stages` 改名为 `recommended_stages`
- 从新推荐源生成推荐关卡集合
- 推荐源失败时允许本地规划不做推荐关卡过滤，或回退到旧缓存
- 删除 `request_data()` 中对 AOG URL 的硬编码

验收标准：
- `plan.calc_mode = local-aog` 不再访问 `arkonegraph.herokuapp.com`
- 旧缓存缺失时仍能生成本地规划结果

### 5.4 阶段四：配置和文档清理

- 将用户可见文案中的 `aog` 逐步改为“推荐源”或“一图流”
- 更新 `README.md` 和 `grass_on_aog/readme.md`
- 可选：保留 `grass_on_aog` 模块名，避免破坏导入路径；仅改描述和内部实现

---

## 6. 缓存策略

推荐缓存文件：

```text
cache/material_recommendation_yituliu.json
cache/material_recommendation_common.json
```

缓存规则：
- 默认按周缓存，沿用当前 `cache_key = '%Y--%V'`
- 强制刷新时重新请求远端
- 远端失败时优先使用未过期缓存
- 未过期缓存缺失但存在旧缓存时，可以告警后继续使用旧缓存

缓存内容应包含：
- `source`
- `fetched_at`
- `exp_coefficient`
- `sample_size`
- `items`
- 原始响应的最小必要字段，便于后续排查

---

## 7. 回退策略

推荐顺序：

1. 使用一图流新版推荐表
2. 使用本地一图流缓存
3. 使用企鹅物流矩阵按主产物期望理智粗略推荐
4. 使用配置项：
   - `normal_action`
   - `no_aog_data_action`
5. 无可用动作时返回 `None`

企鹅物流粗略兜底算法首版可以只做：

```text
目标材料 itemId
  -> 找到包含该 itemId 的关卡掉落记录
  -> 过滤样本数低于阈值的记录
  -> 用 apCost / (quantity / times) 排序
  -> 选期望理智最低的普通或活动关卡
```

这不是完整综合效率，但比直接失败更好，并且实现成本低。

---

## 8. 测试计划

单元测试：
- 一图流响应 fixture 能正确解析出 `itemName -> stageCode`
- 字段缺失时给出明确错误
- 缓存命中时不发网络请求
- 推荐源失败时能回退到缓存或企鹅物流粗略推荐

集成测试：
- `GrassAddOn.choose_stage()` 在模拟库存下返回预期关卡
- `prefer_activity_stage=True` 时优先选择活动关卡
- 排除材料配置仍生效

手动验证：
- 删除推荐缓存后运行一次 `choose_stage()`
- 断网或阻断一图流后确认能按缓存降级
- 配置 `no_aog_data_action=none` 时确认不会误启动刷图

---

## 9. 开放问题

- 一图流 `expCoefficient=0.633` 是否应固定跟随前端默认值，还是做成配置项
- 一图流推荐结果中材料系列与游戏内具体 T3 itemId 的映射是否稳定
- `MaterialPlanning` 是否仍需要“推荐关卡过滤”，还是可以改为纯本地规划
- 是否要保留 `load_aog_data()` 作为兼容函数，还是直接废弃并改所有调用方

---

## 10. 首版建议

首版不要一次性重写规划器。建议先完成：

- 新增一图流新版推荐源适配层
- 替换 `grass` 的推荐表获取逻辑
- 移除 `grass` 运行时对 `arkonegraph.herokuapp.com` 的访问
- 保留模块名和命令名不变

这样改动面最小，可以先恢复一键长草，再逐步清理 `MaterialPlanning` 和文档里的 AOG 命名。
