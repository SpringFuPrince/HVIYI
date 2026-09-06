from modelscope.hub.snapshot_download import snapshot_download

path = r'../asset/models/bge-m3'
model_dir = snapshot_download('BAAI/bge-m3', cache_dir=path)
print(f"模型已下载到: {model_dir}")