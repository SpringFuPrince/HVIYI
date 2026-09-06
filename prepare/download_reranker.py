from modelscope.hub.snapshot_download import snapshot_download

path = r'../asset/models/bge-m3'

snapshot_download(
    model_id="BAAI/bge-reranker-large",
    cache_dir=path,
)

print("下载完成，模型目录：", path)
