"""Spike: PointsYeah pure-HTTP search (Cognito refresh -> AES create_task -> poll fetch_result)."""
import base64, json, sys, time, pathlib
import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CLIENT_ID = "3im8jrentts1pguuouv5s57gfu"
API = "https://api2.pointsyeah.com"
KEY_PREFIX, KEY_SUFFIX, DEFAULT_SECTION = "LefjQ2pEXmiy/nNZvhJ43i8", "YHYbn1hOuAgA=", "hJuaknzb"
IV = b"1020304050607080"
HDR = {
    "content-type": "application/json",
    "origin": "https://www.pointsyeah.com",
    "referer": "https://www.pointsyeah.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
}

def jwt_payload(t):
    p = t.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))

def enc(plain, section):
    key = base64.b64decode(KEY_PREFIX + section + KEY_SUFFIX)
    padder = padding.PKCS7(128).padder()
    data = padder.update(plain.encode()) + padder.finalize()
    e = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
    return base64.b64encode(e.update(data) + e.finalize()).decode()

refresh = (pathlib.Path.home() / ".flypoints" / "pointsyeah_token").read_text().strip()
r = httpx.post("https://cognito-idp.us-east-1.amazonaws.com/", headers={
    "Content-Type": "application/x-amz-json-1.1",
    "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"},
    json={"AuthFlow": "REFRESH_TOKEN_AUTH", "ClientId": CLIENT_ID, "AuthParameters": {"REFRESH_TOKEN": refresh}}, timeout=30)
print("cognito", r.status_code, r.text[:200] if r.status_code != 200 else "ok")
r.raise_for_status()
id_token = r.json()["AuthenticationResult"]["IdToken"]
pl = jwt_payload(id_token)
section = pl["jti"][:8]
print("idtoken exp", pl.get("exp"), "jti section len", len(section))

dep, arr, d1, d2 = sys.argv[1:5]
plain = json.dumps({"search_type": "one_way", "cabins": ["Economy", "Premium Economy", "Business", "First"],
    "segments": [{"arrival": arr, "departure": dep, "departure_date": {"from": d1, "to": d2}}],
    "passengers_v2": {"adults": 1, "children": 0}, "source": "mobile"}, separators=(",", ":"))
h = {**HDR, "authorization": id_token}
c = httpx.post(f"{API}/flight/search/create_task", headers=h,
               json={"data": enc(plain, DEFAULT_SECTION), "encrypted": enc(plain, section)}, timeout=30)
print("create_task", c.status_code, c.text[:300])
task = c.json()["data"]["task_id"]
snaps, t0 = [], time.time()
while time.time() - t0 < 120:
    s = httpx.post(f"{API}/flight/search/fetch_result", headers=h, json={"task_id": task}, timeout=60).json()
    snaps.append(s)
    st = (s.get("data") or {}).get("status")
    n = len((s.get("data") or {}).get("result") or [])
    print(f"{time.time()-t0:5.1f}s status={st} programs={n}")
    if st in ("done", "completed"):
        break
    time.sleep(0.5)
out = pathlib.Path(sys.argv[5])
out.write_text(json.dumps({"query": json.loads(plain), "snapshots": snaps}, indent=1))
print("saved", out)
