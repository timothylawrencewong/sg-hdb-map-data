// Miji Price Estimate - Framer code component
// Paste into Framer: Assets > Code > New file "MijiPriceEstimate.tsx", then drag it onto the Check a Unit page.
// Reads the ML estimates published by sg-hdb-map-data (no API key, no AI tokens).
// URL params it understands (optional): ?block=542&street=Jelapang%20Rd&type=5-room&floor=10

import * as React from "react"
import { addPropertyControls, ControlType } from "framer"

const BASES = [
    "https://timothylawrencewong.github.io/sg-hdb-map-data/estimates/",
    "https://raw.githubusercontent.com/timothylawrencewong/sg-hdb-map-data/main/estimates/",
]
const cache: Record<string, any> = {}
async function getJson(name: string) {
    if (cache[name]) return cache[name]
    for (const b of BASES) {
        try {
            const r = await fetch(b + name)
            if (r.ok) return (cache[name] = await r.json())
        } catch (_e) {}
    }
    throw new Error("Couldn't load " + name)
}

const ABBR: Record<string, string> = {
    avenue: "ave", street: "st", road: "rd", park: "pk", drive: "dr", crescent: "cres", central: "ctrl",
    north: "nth", south: "sth", upper: "upp", lorong: "lor", jalan: "jln", close: "cl", place: "pl",
    bukit: "bt", kampong: "kg", terrace: "ter", heights: "hts", garden: "gdn", gardens: "gdns", tanjong: "tg",
}
const norm = (s: string) =>
    String(s || "").toLowerCase().replace(/[^a-z0-9' ]/g, " ").split(/\s+/).filter(Boolean).map((w) => ABBR[w] || w)
const money = (n: number) => (n >= 1e6 ? "$" + (n / 1e6).toFixed(2) + "M" : "$" + Math.round(n / 1000).toLocaleString("en-SG") + "K")
const ymLabel = (ym: number) =>
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][(ym % 100) - 1] + " " + Math.floor(ym / 100)

type Est = { blk: string; st: string; type: string; model: string | null; sqm: number; lease: string; sales_12m: number; storeys: Record<string, number[]> }

type CondoEst = { project: string; street: string; type: string; tenure: string; sales_12m: number; sizes: Record<string, Record<string, number[]>> }
const sqft = (sqm: number) => Math.round((sqm * 10.7639) / 10) * 10

export default function MijiPriceEstimate(props: { navy: string; cream: string; brass: string; accuracyLine: string }) {
    const { navy, cream, brass, accuracyLine } = props
    const q = typeof window !== "undefined" ? new URLSearchParams(window.location.search) : new URLSearchParams()
    const [block, setBlock] = React.useState(q.get("block") || "")
    const [street, setStreet] = React.useState(q.get("street") || "")
    const [floor, setFloor] = React.useState(q.get("floor") || "")
    const [type, setType] = React.useState(q.get("type") || "")
    const [mode, setMode] = React.useState<"hdb" | "condo">(q.get("project") ? "condo" : "hdb")
    const [project, setProject] = React.useState(q.get("project") || "")
    const [names, setNames] = React.useState<string[]>([])
    const [cstate, setCstate] = React.useState<{ loading?: boolean; error?: string; asOf?: number; rows?: CondoEst[] }>({})
    const [state, setState] = React.useState<{ loading?: boolean; error?: string; town?: string; asOf?: number; rows?: Est[] }>({})

    const run = React.useCallback(async () => {
        const b = block.trim().toUpperCase().replace(/^BLK\.?\s*/, "")
        const toks = norm(street)
        if (!b || !toks.length) return setState({ error: "Enter the block number and street." })
        setState({ loading: true })
        try {
            const idx = await getJson("index.json")
            const key = Object.keys(idx.blocks).find((k) => {
                const [kb, kst] = k.split("|")
                if (kb.toUpperCase() !== b) return false
                const st = norm(kst)
                return toks.every((t) => st.some((w) => w === t || w.startsWith(t)))
            })
            if (!key) return setState({ error: "No estimate for that block yet. Estimates cover blocks with at least one resale in the last 12 months." })
            const [kb, kst] = key.split("|")
            const town = await getJson(idx.blocks[key] + ".json")
            const rows: Est[] = town.blocks.filter((e: Est) => e.blk === kb && e.st === kst && (!type || e.type === type))
            if (!rows.length) return setState({ error: "No estimate for that flat type in this block." })
            setState({ town: town.town, asOf: town.as_of, rows })
        } catch (e: any) {
            setState({ error: "Couldn't load estimates just now. Please try again." })
        }
    }, [block, street, type])

    React.useEffect(() => { if (block && street) run() }, [])

    React.useEffect(() => {
        if (mode !== "condo" || names.length) return
        getJson("condo/index.json").then((i) => setNames(Object.keys(i.projects).sort())).catch(() => {})
    }, [mode])

    const runCondo = React.useCallback(async () => {
        const name = project.trim().toUpperCase()
        if (!name) return setCstate({ error: "Enter the condo or EC name." })
        setCstate({ loading: true })
        try {
            const idx = await getJson("condo/index.json")
            const key = idx.projects[name] ? name : Object.keys(idx.projects).find((k) => k.includes(name))
            if (!key) return setCstate({ error: "No estimate for that project yet. Estimates cover condos, apartments and ECs with at least one resale or sale in the last 12 months." })
            const dist = await getJson("condo/d" + idx.projects[key] + ".json")
            const rows: CondoEst[] = dist.projects.filter((p: CondoEst) => p.project.toUpperCase() === key)
            setCstate({ asOf: dist.as_of, rows })
        } catch (e: any) {
            setCstate({ error: "Couldn't load estimates just now. Please try again." })
        }
    }, [project])

    React.useEffect(() => { if (project) runCondo() }, [])

    const f = parseInt(floor, 10)
    const inBand = (band: string) => !f || (f >= +band.slice(0, 2) && f <= +band.slice(-2))
    const input: React.CSSProperties = { padding: "10px 12px", border: `1px solid ${navy}33`, borderRadius: 8, fontSize: 15, background: "#fff", color: navy, minWidth: 0, flex: 1 }

    return (
        <div style={{ background: cream, color: navy, padding: 20, borderRadius: 14, fontFamily: "Inter, system-ui, sans-serif", width: "100%", maxWidth: 760, margin: "0 auto", boxSizing: "border-box" }}>
            <div style={{ fontSize: 13, letterSpacing: 1, textTransform: "uppercase", color: brass, fontWeight: 600 }}>Miji Price Estimate</div>
            <div style={{ fontSize: 22, fontWeight: 700, margin: "4px 0 12px" }}>What could this home sell for today?</div>
            <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
                {(["hdb", "condo"] as const).map((m) => (
                    <button key={m} onClick={() => setMode(m)} style={{ padding: "7px 14px", borderRadius: 999, border: `1px solid ${navy}`, background: mode === m ? navy : "transparent", color: mode === m ? cream : navy, fontSize: 14, fontWeight: 600, cursor: "pointer" }}>
                        {m === "hdb" ? "HDB" : "Condo / EC"}
                    </button>
                ))}
            </div>
            {mode === "condo" && (
                <div>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                        <input list="miji-condo-names" style={{ ...input, flex: "1 1 240px" }} placeholder="Condo or EC name, e.g. Blossom Residences" value={project} onChange={(e) => setProject(e.target.value)} />
                        <datalist id="miji-condo-names">{project.length >= 2 && names.filter((n) => n.includes(project.trim().toUpperCase())).slice(0, 30).map((n) => <option key={n} value={n} />)}</datalist>
                        <input style={{ ...input, flex: "0 1 90px" }} placeholder="Floor" inputMode="numeric" value={floor} onChange={(e) => setFloor(e.target.value)} />
                        <button onClick={runCondo} style={{ padding: "10px 18px", border: 0, borderRadius: 8, background: navy, color: cream, fontSize: 15, fontWeight: 600, cursor: "pointer" }}>Estimate</button>
                    </div>
                    {cstate.loading && <div style={{ marginTop: 16, opacity: 0.7 }}>Working it out...</div>}
                    {cstate.error && <div style={{ marginTop: 16 }}>{cstate.error}</div>}
                    {cstate.rows && cstate.rows.map((p) => (
                        <div key={p.project + p.type + p.tenure} style={{ marginTop: 14, background: "#fff", borderRadius: 10, padding: 14 }}>
                            <div style={{ fontWeight: 600 }}>{p.project}</div>
                            <div style={{ fontSize: 13, opacity: 0.75, margin: "2px 0 8px" }}>{p.street} · {p.type} · {p.tenure} · {p.sales_12m} sale{p.sales_12m === 1 ? "" : "s"} in the last 12 months</div>
                            {Object.entries(p.sizes).map(([sz, bands]) => {
                                const rows = Object.entries(bands).filter(([b]) => !f || (f >= +b.slice(0, 2) && f <= +b.slice(-2)))
                                return (
                                    <div key={sz} style={{ marginTop: 6 }}>
                                        <div style={{ fontSize: 13, fontWeight: 600, marginTop: 6 }}>About {sqft(+sz).toLocaleString("en-SG")} sqft ({sz} sqm)</div>
                                        {rows.length ? rows.map(([b, v]) => (
                                            <div key={b} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderTop: `1px solid ${navy}14` }}>
                                                <span>Floors {+b.slice(0, 2)}–{+b.slice(-2)}</span>
                                                <span><b>{money(v[0])} – {money(v[2])}</b><span style={{ opacity: 0.6 }}> (mid {money(v[1])})</span></span>
                                            </div>
                                        )) : <div style={{ fontSize: 14 }}>No estimate for that floor.</div>}
                                    </div>
                                )
                            })}
                        </div>
                    ))}
                    {cstate.rows && (
                        <div style={{ fontSize: 12, opacity: 0.75, marginTop: 14, lineHeight: 1.5 }}>
                            Machine-learning price estimate for a resale, not a valuation. Estimated from URA private residential transactions since October 2021 using the project, district, unit size, floor, tenure and remaining lease, location and recent market movement. In August to October 2026 it was within 10% of the actual price for about 87 in 100 condo resales. New launches, unit layout, facing, view and condition can move the real price. As of {ymLabel(cstate.asOf!)}.
                        </div>
                    )}
                </div>
            )}
            {mode === "hdb" && (<div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                <input style={{ ...input, flex: "0 1 90px" }} placeholder="Block" value={block} onChange={(e) => setBlock(e.target.value)} />
                <input style={{ ...input, flex: "1 1 180px" }} placeholder="Street, e.g. Jelapang Rd" value={street} onChange={(e) => setStreet(e.target.value)} />
                <select style={{ ...input, flex: "0 1 130px" }} value={type} onChange={(e) => setType(e.target.value)}>
                    <option value="">All flat types</option>
                    {["2-room", "3-room", "4-room", "5-room", "Executive"].map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
                <input style={{ ...input, flex: "0 1 90px" }} placeholder="Floor" inputMode="numeric" value={floor} onChange={(e) => setFloor(e.target.value)} />
                <button onClick={run} style={{ padding: "10px 18px", border: 0, borderRadius: 8, background: navy, color: cream, fontSize: 15, fontWeight: 600, cursor: "pointer" }}>Estimate</button>
            </div>

            {state.loading && <div style={{ marginTop: 16, opacity: 0.7 }}>Working it out...</div>}
            {state.error && <div style={{ marginTop: 16 }}>{state.error}</div>}
            {state.rows && (
                <div style={{ marginTop: 18 }}>
                    <div style={{ fontWeight: 600 }}>Blk {state.rows[0].blk} {state.rows[0].st}, {state.town}</div>
                    {state.rows.map((e) => {
                        const bands = Object.entries(e.storeys).filter(([b]) => inBand(b))
                        return (
                            <div key={e.type + e.model} style={{ marginTop: 12, background: "#fff", borderRadius: 10, padding: 14 }}>
                                <div style={{ fontWeight: 600 }}>{e.type}{e.model ? ` · ${e.model}` : ""}</div>
                                <div style={{ fontSize: 13, opacity: 0.75, margin: "2px 0 8px" }}>
                                    About {Math.round(e.sqm)} sqm · {e.lease} lease left · {e.sales_12m} resale{e.sales_12m === 1 ? "" : "s"} in the last 12 months
                                </div>
                                {bands.length ? bands.map(([band, v]) => (
                                    <div key={band} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderTop: `1px solid ${navy}14` }}>
                                        <span>Floors {+band.slice(0, 2)}–{+band.slice(-2)}</span>
                                        <span><b>{money(v[0])} – {money(v[2])}</b><span style={{ opacity: 0.6 }}> (mid {money(v[1])})</span></span>
                                    </div>
                                )) : <div style={{ fontSize: 14 }}>No estimate for that floor. Floors with estimates: {Object.keys(e.storeys).map((b) => `${+b.slice(0, 2)}–${+b.slice(-2)}`).join(", ")}.</div>}
                            </div>
                        )
                    })}
                    <div style={{ fontSize: 12, opacity: 0.75, marginTop: 14, lineHeight: 1.5 }}>
                        Machine-learning price estimate, not a valuation. Estimated from HDB resale transactions since 2017 (data.gov.sg) using town, flat type and model, floor area, storey, remaining lease and location. {accuracyLine} Actual prices depend on condition, renovation, facing, view and the market. As of {ymLabel(state.asOf!)}.
                    </div>
                </div>
            )}
            </div>)}
        </div>
    )
}

MijiPriceEstimate.defaultProps = {
    navy: "#1C2B3A",
    cream: "#F4EEE2",
    brass: "#B4863B",
    accuracyLine: "In October 2026 it was within 5% of the actual price for 3 in 4 resales.",
}

addPropertyControls(MijiPriceEstimate, {
    navy: { type: ControlType.Color, title: "Navy" },
    cream: { type: ControlType.Color, title: "Cream" },
    brass: { type: ControlType.Color, title: "Brass" },
    accuracyLine: { type: ControlType.String, title: "Accuracy line", displayTextArea: true },
})
