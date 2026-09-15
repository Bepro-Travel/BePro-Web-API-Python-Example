import json, os, sys, time
from datetime import datetime, timedelta
import requests

BASE_URL = os.environ["V23_BASE_URL"].rstrip("/")
CLIENT_USER = os.environ["V23_CLIENT_USER"]
CLIENT_PASS = os.environ["V23_CLIENT_PASS"]
USER_AGENT = os.environ.get(
    "V23_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
)

def find_value(obj, *substrings, recursive=False):
    """First key in `obj` whose lower-cased name contains all given substrings
    (camelCase can lower-case acronym-heavy names like HTUKey unpredictably)."""
    if not isinstance(obj, dict):
        return None, None
    for k, v in obj.items():
        if all(s in k.lower() for s in substrings):
            return k, v
    if recursive:
        for v in obj.values():
            if isinstance(v, dict):
                nk, nv = find_value(v, *substrings, recursive=True)
                if nk is not None:
                    return nk, nv
    return None, None

# 1. GetAPIBearer -----------------------------------------------------------
resp = requests.get(f"{BASE_URL}/backend/Account/GetAPIBearer",
                     auth=(CLIENT_USER, CLIENT_PASS), headers={"User-Agent": USER_AGENT})
resp.raise_for_status()
bearer = resp.json() if resp.text.startswith('"') else resp.text.strip()

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {bearer}", "User-Agent": USER_AGENT})

# 2-3. Destination search ----------------------------------------------------
r = session.post(f"{BASE_URL}/backend/Utils/GetGooglePrediction",
                  json={"query": "Berlin, Germany", "prefix": "htl"}).json()
place_id = json.loads(r["googleResponse"])["predictions"][0]["place_id"]

r = session.post(f"{BASE_URL}/backend/Utils/GetGooglePredictionDetails",
                  json={"placeId": place_id}).json()
place = json.loads(r["googleResponse"])["result"]
loc = place["geometry"]["location"]
country = next(c["short_name"] for c in place["address_components"] if "country" in c["types"])
destination = {"city": place.get("name") or place["formatted_address"],
               "countryCode": country.upper(), "lat": loc["lat"], "lon": loc["lng"]}

# 4. BeginHotelSearch ---------------------------------------------------------
check_in = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
check_out = (datetime.now() + timedelta(days=33)).strftime("%Y-%m-%d")
r = session.post(f"{BASE_URL}/backend/Hotels/BeginHotelSearch", json={
    "checkIn": check_in, "checkOut": check_out, "starRating": 0,
    "nationality": "IL", "searchRadius": 8, "roomsString": "2;", "recaptchaToken": "",
    "city": destination["city"], "id": destination["countryCode"],
    "lat": destination["lat"], "lon": destination["lon"],
}).json()
search_token, sys_token = r["searchToken"], r["sysToken"]

# 5. GetHotels (poll) ----------------------------------------------------------
hotels = []
deadline = time.time() + 60
while time.time() < deadline:
    r = session.post(f"{BASE_URL}/backend/Hotels/GetHotels",
                      json={"searchToken": search_token, "sysToken": sys_token}).json()
    hotels.extend(r.get("hotels") or [])
    if r.get("poolingFinished"):
        break
    time.sleep(1.5)

# Pick the first hotel/room with a usable hotelUkey/roomBToken
selection = None
for hotel in hotels:
    _, hotel_ukey = find_value(hotel, "ukey")
    _, room_classes = find_value(hotel, "roomclass")
    if not hotel_ukey or not room_classes:
        continue
    _, item = find_value(hotel, "item")
    _, item_code = find_value(item or {}, "code")
    for room in room_classes:
        _, hotel_rooms = find_value(room, "hotelroom")
        if not hotel_rooms:
            continue
        _, btoken = find_value(hotel_rooms[0], "token")
        if btoken:
            selection = {"hotelUkey": hotel_ukey, "roomBToken": btoken,
                         "hotelItemCode": item_code or ""}
            break
    if selection:
        break
if not selection:
    sys.exit("No bookable hotel/room found for this search")

# 6-7. HotelInfo / ChargeConditions (optional, informational) -----------------
session.post(f"{BASE_URL}/backend/Hotels/HotelInfo", params={
    "searchToken": search_token, "hotelUkey": selection["hotelUkey"],
    "RoomBToken": selection["roomBToken"],
})
session.post(f"{BASE_URL}/backend/Hotels/ChargeConditions", json={
    "searchToken": search_token, "roomBTokenList": [selection["roomBToken"]], "language": "He",
})

# 8. BeginOneHotelSearch --------------------------------------------------------
r = session.post(f"{BASE_URL}/backend/Hotels/BeginOneHotelSearch", json={
    "searchToken": search_token,
    "hotels": [{"roomBToken": selection["roomBToken"],
                "hotelItemCode": selection["hotelItemCode"], "multiRoomsBTokens": []}],
}).json()
one_hotel_token = r["searchToken"]

# 9. GetOneHotelSearchData (poll) + refresh selection ---------------------------
refreshed = {**selection, "roomId": ""}
deadline = time.time() + 60
while time.time() < deadline:
    r = session.post(f"{BASE_URL}/backend/Hotels/GetOneHotelSearchData",
                      json={"searchToken": one_hotel_token}).json()
    pool = r.get("hotels") or []
    if pool:
        _, hotel_obj = find_value(pool[0], "hotel", recursive=True)
        hotel_obj = hotel_obj or pool[0]
        _, ukey = find_value(hotel_obj, "ukey", recursive=True)
        _, room_classes = find_value(hotel_obj, "roomclass", recursive=True)
        room = room_classes[0] if room_classes else None
        _, btoken = find_value(room, "token") if room else (None, None)
        _, room_id = find_value(room, "uniquekey") if room else (None, None)
        refreshed = {"hotelUkey": ukey or selection["hotelUkey"],
                     "roomBToken": btoken or selection["roomBToken"],
                     "roomId": room_id or ""}
    if r.get("poolingFinished"):
        break
    time.sleep(1.5)

# 10. Book (HELD, unpaid, unconfirmed) -------------------------------------------
r = session.post(f"{BASE_URL}/backend/Hotels/Book", params={
    "hotelUkey": refreshed["hotelUkey"], "searchToken": one_hotel_token,
    "roomId": refreshed["roomId"], "Language": "He", "afterOneHotelSearch": "true",
}).json()
print(f"Held OrderId: {r['orderId']}  totalPrice={r.get('totalPrice')} {r.get('currency')}")

# Step 11 (BookComplete) intentionally NOT called here - see the wiki's BookComplete page.
