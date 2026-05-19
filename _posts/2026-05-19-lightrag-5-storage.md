---
title: 三层存储架构——KV、向量、图数据库如何协同
author: sanyinchen
date: 2026-05-19
categories: [ AI ]
tags: [LightRAG, RAG, 存储架构]
render_with_liquid: false
toc: true
---

上一篇我们走完了整条索引流水线——文档从切块到抽取到合并入图，最后一个自然的问题是：这些数据到底存在哪？

如果你刚上手 LightRAG，可能会被 `lightrag/kg/` 下面十几个文件吓一跳。JSON、Redis、PostgreSQL、Neo4j、Milvus、MongoDB、OpenSearch……一锅粥。但把"三层存储"这条线捋清楚，所有后端都能对号入座。

这一篇就干这件事——从接口到实现，从单机到分布式，把 LightRAG 的存储层拆开揉碎。

## 一、先看清楚：四类存储，而不是三种

LightRAG 的 `base.py` 定义了四个抽象基类，这是整个存储层的骨架：

| 存储类型 | 基类 | 存什么 | 实例个数 |
|---|---|---|---|
| KV 存储 | `BaseKVStorage` | 原文、切块、实体关系全量数据、缓存 | 7 个 |
| 向量存储 | `BaseVectorStorage` | 实体向量、关系向量、chunk 向量 | 3 个 |
| 图存储 | `BaseGraphStorage` | 实体节点、关系边、邻接关系 | 1 个 |
| 文档状态 | `DocStatusStorage` | 每个 doc/chunk 的处理状态 | 1 个 |

一共 12 个存储实例。不是说"三层"就三个——KV 那一层有 7 个命名空间，向量有 3 个独立索引。后面逐个拆。


## 二、四类 BaseStorage 的接口设计

入口在 `base.py`。四个基类都是 `@dataclass`，都继承 `StorageNameSpace`（ABC），纯粹是抽象方法堆起来的接口层。这种设计的好处是——后端实现可以完全不关心上层业务逻辑，只要把接口实现完，就能被 LightRAG 用。

### BaseKVStorage（`base.py:356`）

最忙的一个。7 个命名空间全靠它。关键方法：

```python
async def get_by_id(self, id: str) -> dict | None
async def get_by_ids(self, ids: list[str]) -> list[dict]
async def filter_keys(self, keys: set[str]) -> set[str]      # 返回不存在的 key
async def upsert(self, data: dict[str, dict]) -> None
async def delete(self, ids: list[str]) -> None
async def is_empty(self) -> bool
```

没有 `query`、没有 `search`——KV 就是朴素 get/put，不做语义检索。

### BaseVectorStorage（`base.py:218`）

向量检索的核心。多了 `embedding_func` 和 `cosine_better_than_threshold`：

```python
async def query(self, query: str, top_k: int,
                query_embedding: list[float] = None) -> list[dict]
async def upsert(self, data: dict[str, dict]) -> None
async def get_by_id / get_by_ids / delete / get_vectors_by_ids...
```

注意 `query` 可以接受预计算的 embedding——这在检索流程里很关键，local/global 模式不会重复算同一份向量。

### BaseGraphStorage（`base.py:405`）

图操作的语义全在这。节点/边的增删查、度数、BFS 子图、标签搜索：

```python
async def upsert_node / upsert_edge / delete_node / remove_nodes / remove_edges
async def has_node / has_edge / get_node / get_edge / node_degree
async def get_knowledge_graph(node_label, max_depth, max_nodes) -> KnowledgeGraph
async def get_all_labels / get_popular_labels / search_labels
```

几乎所有边操作都注明了 **undirected**——LightRAG 把图当成无向图处理，这在合并实体关系时统一做了 `sorted([src, tgt])` 归一化。

### DocStatusStorage（`base.py:810`）

继承自 `BaseKVStorage`，额外加了文档状态管理的方法：

```python
async def get_status_counts() -> dict[str, int]
async def get_docs_by_status(status: DocStatus) -> dict
async def get_docs_paginated(...) -> tuple[list, int]
```

状态流转：`pending → processing → processed (或 failed)`。这套状态机是断点续跑的基础——进程挂了重启，pending 和 processing 状态的文档会被重新拾起。


## 三、KV 存储：7 个命名空间各司其职

看 `lightrag.py:663-704`，初始化阶段一口气创建了 7 个 KV 实例，用的都是同一个 `key_string_value_json_storage_cls`：

| 命名空间 | 存什么 | 关键数据结构 |
|---|---|---|
| `kv_store_full_docs` | 原始文档全文 | `{content, file_path, ...}` |
| `kv_store_text_chunks` | 切块结果 | `{content, tokens, chunk_order_index, full_doc_id, file_path}` |
| `kv_store_full_entities` | 实体全量数据 | `{entity_name, content, description, source_id, file_path}` |
| `kv_store_full_relations` | 关系全量数据 | `{keywords, source_id, description, weight, file_path}` |
| `kv_store_entity_chunks` | 实体→chunk 映射 | 记录每个实体出现在哪些 chunk |
| `kv_store_relation_chunks` | 关系→chunk 映射 | 记录每条关系被哪些 chunk 发现 |
| `kv_store_llm_response_cache` | LLM 调用缓存 | `{return, cache_type, mode, ...}` |

后两个 mapping 是删文档时反向查的——知道 doc 关联的 chunks，就知道要清理哪些实体/关系。

### JsonKVStorage 的实现细节

默认后端是 `JsonKVStorage`（`json_kv_impl.py`）。核心就一个 `self._data: dict`——所有数据在内存里，写到 `kv_store_{namespace}.json`。但有一个关键设计：

**写操作只写内存，不写磁盘**。`upsert` 只更新 `self._data` 和设置 `storage_updated` 标记（多进程共享的 `multiprocessing.Value`）。真正的落盘延迟到 `index_done_callback`——这在整个索引批次完成后才触发，一把写到 JSON 文件。

这样设计的原因：索引过程非常频繁地写 KV（每个 chunk 都要更新），如果每次 upsert 都写磁盘，IO 会炸。批完再写，干净利落。

### 其他后端选择

- **RedisKVStorage**：数据在 Redis，适合多进程/多节点共享。自带持久化。
- **PGKVStorage**：数据在 PostgreSQL 的一张 kv 表里，适合需要 SQL 查询的团队。
- **MongoKVStorage**：MongoDB，文档模型的天然契合。
- **OpenSearchKVStorage**：OpenSearch 兼容 Elasticsearch 生态，适合已有 ES 集群的场景。

选后端就是选持久化介质，接口完全一致。


## 四、向量存储：三套独立索引

`lightrag.py:712-729` 初始化了 3 个向量库：

| 命名空间 | 存什么 | meta_fields |
|---|---|---|
| `vector_store_entities` | 实体向量 | entity_name, source_id, content, file_path |
| `vector_store_relationships` | 关系向量 | src_id, tgt_id, source_id, content, file_path |
| `vector_store_chunks` | chunk 向量 | full_doc_id, content, file_path |

为什么三个独立？因为检索模式不同时，查询的目标向量库不同：

- **local 模式**：查询 entity 向量和 relation 向量，找到相关实体/关系后再通过图找邻居。
- **global 模式**：直接用 relation 向量检索全局关系。
- **naive 模式**：查 chunk 向量，走传统的 chunk→context 路线。

### NanoVectorDBStorage：默认的轻量实现

源码在 `nano_vector_db_impl.py`。底层依赖 `nano-vectordb`，一个纯 Python 的本地向量库。值得说的几个细节：

**向量压缩**（`nano_vector_db_impl.py:132-134`）：

```python
vector_f16 = embeddings[i].astype(np.float16)   # float32 → float16
compressed_vector = zlib.compress(vector_f16.tobytes())  # zlib 压缩
encoded_vector = base64.b64encode(compressed_vector).decode("utf-8")  # base64
```

float32 的 3072 维向量大概是 12KB，经过 float16 + zlib + base64 之后大幅度瘦身。存 JSON 文件不心疼。

**批量 embedding**：`upsert` 里先 `asyncio.gather` 并发算 embedding（在锁外面），再一次性 upsert 进 NanoVectorDB。锁只锁写入的瞬间，embedding 计算不阻塞其他操作。

### 生产环境向量后端

- **MilvusVectorDBStorage**：支持 HNSW、IVF、DISKANN 等多种索引，量化存储（SQ/PQ），适合百万级以上向量。
- **PGVectorStorage**：PostgreSQL + pgvector 扩展，SQL 一把梭。
- **FaissVectorDBStorage**：Meta 出品，纯本地，速度快。
- **QdrantVectorDBStorage**：Rust 写的向量库，带过滤和 payload。
- **MongoVectorDBStorage**：MongoDB Atlas 的向量能力。
- **OpenSearchVectorDBStorage**：ES 生态的向量检索。


## 五、图存储：节点和边背后是什么

`chunk_entity_relation_graph` 是唯一一个图存储实例（`lightrag.py:706`）。存的就是抽取出的实体（node）和关系（edge）。初始化后，LightRAG 不区分"这个图是 NetworkX 还是 Neo4j"——反正都是 `BaseGraphStorage`。

### NetworkXStorage：默认的单机方案

`networkx_impl.py`。底层是一个 `nx.Graph()` 对象，持久化到 GraphML 文件。跟 JsonKVStorage 一样的延迟写盘策略——`upsert_node/upsert_edge` 只操作内存中的 nx 图，`index_done_callback` 才 `nx.write_graphml`。

`get_knowledge_graph` 方法值得单讲（`networkx_impl.py:334-509`）：

- 从指定节点出发做 BFS，优先选高度数节点
- 按 `max_depth` 控制深度，`max_nodes` 控制节点数
- 返回 `KnowledgeGraph` 对象，带 `is_truncated` 标记
- 通过 `sorted([src, tgt])` 保证无向边不重复

这就是查询时 local 模式"从一个实体出发找相关子图"的底层实现。

### 生产环境图后端

- **Neo4JStorage**：图数据库的标杆，Cypher 查询，支持大规模图。有重试机制（tenacity），带了连接池管理。
- **PGGraphStorage**：PostgreSQL + AGE 扩展，用 SQL 操作图。
- **MemgraphStorage**：内存图数据库，性能极好。
- **MongoGraphStorage** / **OpenSearchGraphStorage**：文档库模拟图存储，适合不便单独维护图数据库的团队。


## 六、DocStatus：被低估的第四层

很多人把 LightRAG 的存储总结成"三层"（KV+向量+图），但其实 DocStatus 值得单列。

`DocStatusStorage` 继承自 `BaseKVStorage`，但多了状态管理和分页查询。默认实现是 `JsonDocStatusStorage`，存到 `kv_store_doc_status.json`。

它管的是**文档处理生命周期**。每个文档进来会被标记成：

```
pending → processing → processed / failed
```

- **pending**：入队了但还没被 worker 领走
- **processing**：worker 正在处理
- **processed**：索引完成，数据已写入各种存储
- **failed**：出错了（比如 LLM 调用超时），带着 `error_msg`

还有一个隐藏状态 `preprocessed`——多模态场景下，"文本抽取完成但图片还没处理"就是这个状态。

这套状态机保证了：
1. 断点续跑：进程挂了重启，从 pending 和 processing 重新开始
2. 去重：同一篇文档的 chunk 按 content hash 算 id，已经 processed 的直接跳过
3. 追踪：`track_id` 让调用方能异步查询进度

DocStatus 也可以切换后端——Redis、PG、MongoDB、OpenSearch 都支持。

## 七、跨进程同步：shared_storage 做了什么

如果你开了多 worker（比如 Gunicorn 多进程模式），LightRAG 怎么保证进程 A 写的东西能被进程 B 读到？

答案在 `shared_storage.py`。它用 `multiprocessing.Manager` 创建跨进程共享的字典和锁：

- **NamespaceLock**：每个命名空间一把锁，保证同一时间只有一个进程写
- **UpdateFlag**（`multiprocessing.Value`）：写操作完成后标记"需要重新加载"
- **共享 Data Dict**：所有进程共享同一份内存数据，通过 `Manager.dict()` 实现

读操作时会检查 `UpdateFlag`：如果发现是 `True`，就从磁盘重新加载数据，再把 flag 复位。这套机制在 `_get_graph()`（NetworkX）、`_get_client()`（NanoVectorDB）里都能看到——先查 flag，再决定是否 reload。

> 写操作只写内存→批完再落盘→其他进程感知到 flag 变化→读取时自动 reload。

这是 LightRAG 单机多进程能跑的关键，也是它工程上最扎实的地方。

## 八、生产环境怎么选后端

生产环境的后端选择、Docker Compose 编排、性能调优，放到第 8 篇《生产环境部署与最佳实践》统一讲。这里记住一件事就够了：**切换后端只改配置，业务代码不用动**。

## 九、总结

LightRAG 的存储层设计有三个核心思路：

1. **接口先于实现**。四类 `BaseStorage` 接口把"需要什么操作"定义清楚，后端实现随意换。
2. **延迟落盘**。写操作只动内存，批完再持久化。索引流程里成百上千次 KV update 不会造成 IO 风暴。
3. **跨进程感知**。`shared_storage` 的 flag 机制让多 worker 之间自动同步，读时 reload，写时不互相踩。

你把这三条吃透，再去 `lightrag/kg/` 下面看任意一个后端实现，会发现它们只是"同一个故事的不同讲法"——Neo4j 的 `upsert_node` 是跑 Cypher，NetworkX 的 `upsert_node` 是调 `graph.add_node`，但接口签名一模一样。

下一篇我们回到检索侧，看看 LightRAG 怎么用高低层关键词、怎么从三层存储里把数据捞出来、Reranker 又是怎么重新排序的。

---

*上一篇：[文档是怎样变成知识图谱的——LightRAG 索引流程全解析]({% post_url 2026-05-19-lightrag-4-indexing %})*

*下一篇：[双层级检索原理——高低层关键词与 Reranker]({% post_url 2026-05-19-lightrag-6-retrieval %})


---

本文由 AgentPlanFlow 生成
