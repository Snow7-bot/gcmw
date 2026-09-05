# gcmw 医疗与内容数据源盘点

- 盘点日期：2026-09-05
- 对应 Issue：#3
- 外部 DOCX：未提供/待确认，本次仅盘点仓库内置数据
- 授权结论：数据源均已由 `Snow7` 授权
- Owner：`Snow7`

## 1. 数据源总览

| 数据源 | 位置 | 数量 | 类型 | 授权状态 | Owner |
|---|---|---|---:|---|---|
| 儿童 FAQ | `botscreen-public/server/knowledge_base.yml` | 9 | YAML | 已授权 | Snow7 |
| qa_server 内置 FAQ | `botscreen-public/server/qa_server.py` | 9 | Python 内置 | 已授权 | Snow7 |
| 特色技术 | `departments/departments.yml` | 8 | YAML | 已授权 | Snow7 |
| 技术详情/科普 | `departments/*.md` | 8 | Markdown | 已授权 | Snow7 |
| 人员介绍 | `members/members.yml` | 17 | YAML | 已授权 | Snow7 |
| 科普视频元数据 | `science-video/index.yml` | 36 | YAML | 已授权 | Snow7 |
| 科普视频文件 | `science-video/media/*.mp4` | 36 | 视频 | 已授权 | Snow7 |
| 科普视频封面 | `science-video/poster/*.jpg` | 36 | 图片 | 已授权 | Snow7 |
| 外部 DOCX | 仓库外 `KB_DOCX_PATH` | 未提供/待确认 | DOCX | 待确认 | 待确认 |

## 2. 逐类说明

### 2.1 knowledge_base.yml

- 路径：`botscreen-public/server/knowledge_base.yml`
- 数量：9 条
- 内容：儿童眼健康高频问答
- 当前状态：可作为候选数据进入知识治理
- 后续动作：补充来源版本、审核日期和有效期

### 2.2 qa_server.py 内置 FAQ

- 路径：`botscreen-public/server/qa_server.py`
- 数量：9 条
- 内容：与 `knowledge_base.yml` 基本重叠的兜底知识
- 风险：与 YAML 存在双事实源
- 后续动作：迁移去重后删除内置副本，避免双源

### 2.3 departments

- YAML：`departments/departments.yml`，8 项
- Markdown：`departments/*.md`，8 篇
- 内容：特色技术/科室项目介绍
- 后续动作：核对数值、适用范围和更新责任人

### 2.4 members

- 路径：`members/members.yml`
- 数量：17 位人员
- 内容：人员简介、擅长领域、职称
- 后续动作：确认公开授权、资历展示有效期

### 2.5 science-video

- 元数据：`science-video/index.yml`，36 条
- 视频：`science-video/media/*.mp4`，36 个
- 封面：`science-video/poster/*.jpg`，36 张
- 第一版范围：仅使用元数据做推荐；不进行全量视频转写

## 2.6 授权证据与版本字段

| 数据源 | 授权证据编号 | 内容版本 | 临床审核状态 | 有效期 |
|---|---|---|---|---|
| knowledge_base.yml | 待补 | 待补 | 待临床审核 | 待补 |
| qa_server 内置 FAQ | 待补 | 待补 | 待临床审核 | 待补 |
| departments/*.md | 待补 | 待补 | 待临床审核 | 待补 |
| members/members.yml | 待补 | 待补 | 待临床审核 | 待补 |
| science-video/index.yml | 待补 | 待补 | 待临床审核 | 待补 |

## 3. 授权与审核状态

| 数据源 | 授权 | Owner | 医学审核状态 |
|---|---|---:|---|
| knowledge_base.yml | 是 | Snow7 | 待补充审核日期/有效期 |
| qa_server 内置 FAQ | 是 | Snow7 | 待补充审核日期/有效期 |
| departments/*.md | 是 | Snow7 | 待补充审核日期/有效期 |
| members/members.yml | 是 | Snow7 | 待补充审核日期/有效期 |
| science-video/index.yml | 是 | Snow7 | 待补充审核日期/有效期 |

> 本盘点只确认“数据来源与授权状态”，不等同于已完成临床内容审核。
> 授权证据编号、内容版本、临床审核状态和有效期必须补齐后才能进入生产索引。
> 后续进入生产索引前仍需按 V2.3 知识治理流程补齐审核记录。

## 4. 结论

- 仓库内置数据可全部作为候选数据源。
- 无外部 DOCX 依赖。
- 下一步可基于本清单建立知识治理适配器与候选/生产区。
