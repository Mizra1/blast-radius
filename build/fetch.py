import io, os, tarfile, urllib.request, shutil
SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
data = urllib.request.urlopen(f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz", timeout=120).read()
os.makedirs("/opt", exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(data)) as t:
    t.extractall("/opt")
shutil.move(f"/opt/sqlglot-{SHA}", "/opt/sqlglot")
