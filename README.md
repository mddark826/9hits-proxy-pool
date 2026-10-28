# 9Hits Proxy Pool — Render-এ auto-refresh

ল্যাপটপ ছাড়া, Render Free-তেই চলার মতো হালকা সার্ভিস। এটা Viewer না, এটা
**proxy দেওয়ার সার্ভিস**। Viewer সেশনগুলো এই সার্ভিস থেকে একটা করে আলাদা
proxy নেবে।

## এটা কী করে

1. প্রতি ২ ঘণ্টায় (REFRESH_MINUTES) পাবলিক proxy লিস্ট থেকে candidate নেয়।
2. `curl` দিয়ে `api.ipify.org`-এর মাধ্যমে প্রতিটা proxy টেস্ট করে, শুধু
   working-গুলো রাখে। লক্ষ্য: ৫০টা live (TARGET_LIVE) — ২০-৩০ সেশনের জন্য
   live সেশন সংখ্যার চেয়ে বেশি থাকতে হবে, নইলে অনেক সেশন Waiting-এ থাকবে।
3. 9Hits-এর অফিসিয়াল ExProxy প্রোটোকল মানে:
   - নতুন proxy চাইলে: `GET /pool?token=...&user=...&ver=...&platform=...&rand=...`
     → `{"type":"socks5","server":"1.2.3.4:1080","id":"1.2.3.4:1080"}`
     অথবা proxy না থাকলে `{"error":"No proxy available"}` (Viewer ৩০ সেকেন্ড
     পর আবার চাইবে)।
   - Viewer-এর রিপোর্ট একই URL-এ আসে: `&status=working|idle|error|released`।
     ১৫ মিনিট কোনো রিপোর্ট না আসলে proxy আবার free ধরা হয়।

## Render-এ deploy (২ মিনিট)

1. এই ফোল্ডারটা GitHub-এ একটা public/private repo হিসেবে push করো।
2. Render Dashboard → New → Web Service → ওই repo বেছে নাও।
   অথবা Blueprint: New → Blueprint → repo বেছে নাও, `render.yaml` থেকেই
   Free plan, Singapore region, Docker সেট হয়ে যাবে।
3. Environment-এ বসাও (optional কিন্তু recommended):
   - `NINEHITS_TOKEN` = তোমার 9Hits Viewer-এর access key (token)
   - `ADMIN_KEY` = নিজের পছন্দের একটা গোপন key
4. Build শেষ হলে ব্রাউজারে খোলো: `https://<service>.onrender.com/health`
   `{"status":"ok","live":...}` দেখলে বুঝবে চলছে। প্রথম refresh-এ ২-৪ মিনিট
   লাগতে পারে, live ০ থেকে বাড়বে।
5. 9Hits Viewer-এ: প্রতিটা সেশনের connection type **ExProxy** করে URL বসাও:
   `https://<service>.onrender.com/pool`
   ২০-৩০টা সেশন সবাই এই একই URL ব্যবহার করবে — পুল আলাদা আলাদা proxy দেবে।

## Render Free-এর সীমা — আগেই জেনে রাখো

- Free সার্ভিস ১৫ মিনিট কোনো inbound traffic না থাকলে ঘুমিয়ে যায়, পরের
  request-এ জাগতে ~৩৫-৫৫ সেকেন্ড লাগে। Viewer চলার সময় প্রতি ~৫ মিনিটে
  রিপোর্ট পাঠায়, তাই তখন জেগে থাকে। চাইলে UptimeRobot দিয়ে প্রতি ১০-১৪
  মিনিটে `/health`-এ ping দিয়ে warm রাখতে পারো।
- ৫১২ MB RAM, ১টা instance, filesystem deploy/spin-down-এ মুছে যায় — তাই
  পুল startup-এ আবার refresh হয়, এটা by design।
- এই সার্ভিসে ২০টা Chromium Viewer চালানোর চেষ্টা করো না — সেটা OOM হবে।
  Viewer আলাদা হোস্টে থাকবে, শুধু proxy এই Render সার্ভিস থেকে আসবে।

## সৎ সতর্কতা

- এগুলো ফ্রি পাবলিক proxy। কোয়ালিটি কম, অনেক campaign proxy allow করে না,
  আর একটা proxy গড়ে কয়েক ঘণ্টা বাঁচে — তাই auto-refresh-ই আসল কাজ।
- Session সংখ্যা ২০-৩০ হলেও earning ২০-৩০ গুণ হবে না। যাচাই করবে Panel →
  Viewer Sessions-এ: IP আলাদা কি না, Status Working কি না, Points বাড়ছে কি না।
- Pool-এর URL / token কাউকে দিও না — দিলে অন্যরা তোমার proxy শেষ করে দেবে।

## লোকাল টেস্ট

```bash
pip install -r requirements.txt
python app.py
# আলাদা টার্মিনালে:
curl http://127.0.0.1:5000/health
curl "http://127.0.0.1:5000/pool?token=test&user=test&session=test1"
curl http://127.0.0.1:5000/stats
```
