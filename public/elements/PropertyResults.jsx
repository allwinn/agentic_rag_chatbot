// Property cards + map shown under an answer. `props` is injected by Chainlit:
// { items: [{title, subtitle, meta, price, image, url, lat, lon, source}], total }
import { useEffect, useRef, useState } from "react";

const gold = "hsl(var(--primary))";
const border = "1px solid hsl(var(--border))";

function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  if (window.__leafletLoading) return window.__leafletLoading;
  window.__leafletLoading = new Promise((resolve, reject) => {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = "/public/vendor/leaflet/leaflet.css";  // bundled, no CDN dependency
    document.head.appendChild(css);
    const js = document.createElement("script");
    js.src = "/public/vendor/leaflet/leaflet.js";
    js.onload = () => resolve(window.L);
    js.onerror = reject;
    document.head.appendChild(js);
  });
  return window.__leafletLoading;
}

function MapView({ points }) {
  const ref = useRef(null);
  useEffect(() => {
    let map;
    loadLeaflet().then((L) => {
      if (!ref.current) return;
      map = L.map(ref.current, { scrollWheelZoom: false, attributionControl: true });
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 18, attribution: "© OpenStreetMap",
      }).addTo(map);
      const bounds = [];
      points.forEach((p, i) => {
        const icon = L.divIcon({
          className: "",
          html: `<div style="background:#A8844F;color:#fff;border-radius:999px;width:24px;height:24px;
                 display:flex;align-items:center;justify-content:center;font:600 12px Inter,sans-serif;
                 border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.35)">${i + 1}</div>`,
          iconSize: [24, 24], iconAnchor: [12, 12],
        });
        L.marker([p.lat, p.lon], { icon })
          .addTo(map)
          .bindPopup(`<b>${p.title}</b><br>${p.price || ""}<br><a href="${p.url}" target="_blank">View listing</a>`);
        bounds.push([p.lat, p.lon]);
      });
      if (bounds.length === 1) map.setView(bounds[0], 14);
      else map.fitBounds(bounds, { padding: [24, 24] });
    }).catch(() => {});
    return () => map && map.remove();
  }, []);
  return <div ref={ref} style={{ height: 280, borderRadius: 10, border, overflow: "hidden" }} />;
}

function Card({ item, n }) {
  const [imgOk, setImgOk] = useState(Boolean(item.image));
  return (
    <a href={item.url} target="_blank" rel="noreferrer"
       style={{ display: "flex", flexDirection: "column", border, borderRadius: 10, overflow: "hidden",
                textDecoration: "none", color: "inherit", background: "hsl(var(--card))" }}>
      <div style={{ position: "relative", aspectRatio: "16 / 10", background: "hsl(var(--muted))" }}>
        {imgOk ? (
          <img src={item.image} alt={item.title} loading="lazy" onError={() => setImgOk(false)}
               style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
        ) : (
          <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center",
                        color: gold, fontFamily: "'Cormorant Garamond', serif", fontSize: 22 }}>
            {item.source === "darglobal" ? "DarGlobal" : "Wasalt"}
          </div>
        )}
        <span style={{ position: "absolute", top: 8, left: 8, background: "rgba(20,18,16,.78)", color: "#F3EDE3",
                       font: "600 10px Inter, sans-serif", letterSpacing: ".12em", textTransform: "uppercase",
                       padding: "4px 7px", borderRadius: 4 }}>
          {item.lat != null ? `${n}. ` : ""}{item.source === "darglobal" ? "DarGlobal" : "Wasalt"}
        </span>
      </div>
      <div style={{ padding: "10px 12px 12px", display: "flex", flexDirection: "column", gap: 3 }}>
        <div style={{ fontFamily: "'Cormorant Garamond', serif", fontSize: 19, fontWeight: 600, color: gold }}>
          {item.price}
        </div>
        <div style={{ fontSize: 13.5, fontWeight: 600, lineHeight: 1.3 }}>{item.title}</div>
        <div style={{ fontSize: 12.5, color: "hsl(var(--muted-foreground))" }}>{item.subtitle}</div>
        {item.meta ? <div style={{ fontSize: 12, color: "hsl(var(--muted-foreground))" }}>{item.meta}</div> : null}
      </div>
    </a>
  );
}

export default function PropertyResults() {
  const items = props.items || [];
  const mapped = items.filter((p) => p.lat != null && p.lon != null);
  const [tab, setTab] = useState("cards");
  const btn = (key, label) => (
    <button onClick={() => setTab(key)}
            style={{ padding: "5px 12px", borderRadius: 999, fontSize: 12.5, cursor: "pointer",
                     border: tab === key ? `1px solid ${gold}` : border,
                     background: tab === key ? "hsl(var(--accent))" : "transparent", color: "inherit" }}>
      {label}
    </button>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 4, width: "100%" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        {btn("cards", `Cards (${items.length})`)}
        {mapped.length ? btn("map", `Map (${mapped.length})`) : null}
        {props.total > items.length ? (
          <span style={{ fontSize: 12, color: "hsl(var(--muted-foreground))" }}>
            showing {items.length} of {props.total} matches
          </span>
        ) : null}
      </div>
      {tab === "cards" ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 12 }}>
          {items.map((it, i) => <Card key={it.url} item={it} n={mapped.indexOf(it) + 1} />)}
        </div>
      ) : (
        <MapView points={mapped} />
      )}
    </div>
  );
}
