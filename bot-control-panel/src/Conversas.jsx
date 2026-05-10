import { useState, useEffect, useCallback } from 'react'
import { supabase } from './lib/supabase'
import { toast } from 'sonner'
import { Search, RefreshCw, ChevronRight, ChevronDown } from 'lucide-react'

export default function Conversas() {
    const [turns, setTurns] = useState([])
    const [loading, setLoading] = useState(false)
    const [filtroUser, setFiltroUser] = useState('')
    const [horas, setHoras] = useState(24)
    const [expandido, setExpandido] = useState({})

    const carregar = useCallback(async () => {
        setLoading(true)
        try {
            const desde = new Date(Date.now() - horas * 3600 * 1000).toISOString()
            let q = supabase
                .from('bot_turns')
                .select('id, user_id, created_at, user_input, final_output, output_format, fallback_used, latency_ms, model, tokens_in, tokens_out, error, tool_calls')
                .gte('created_at', desde)
                .order('created_at', { ascending: false })
                .limit(200)
            if (filtroUser) q = q.like('user_id', `%${filtroUser}%`)
            const { data, error } = await q
            if (error) throw error
            setTurns(data || [])
        } catch (e) {
            toast.error('Erro: ' + e.message)
        } finally {
            setLoading(false)
        }
    }, [filtroUser, horas])

    useEffect(() => { carregar() }, [carregar])

    function toggle(id) {
        setExpandido(p => ({ ...p, [id]: !p[id] }))
    }

    const stats = useState(() => ({ total: 0, json: 0, fallback: 0, tokens: 0 }))[0]
    const total = turns.length
    const json = turns.filter(t => t.output_format === 'json').length
    const fallback = turns.filter(t => t.fallback_used).length
    const tokensTotal = turns.reduce((s, t) => s + ((t.tokens_in || 0) + (t.tokens_out || 0)), 0)
    const latMedia = total ? Math.round(turns.reduce((s, t) => s + (t.latency_ms || 0), 0) / total) : 0

    return (
        <div className="p-6 space-y-4">
            <div className="flex flex-wrap items-center gap-3">
                <div className="relative">
                    <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                    <input
                        type="text"
                        placeholder="Filtrar user_id"
                        value={filtroUser}
                        onChange={e => setFiltroUser(e.target.value)}
                        className="pl-9 border rounded px-3 py-2 dark:bg-slate-800"
                    />
                </div>
                <select value={horas} onChange={e => setHoras(parseInt(e.target.value))} className="border rounded px-3 py-2 dark:bg-slate-800">
                    <option value={1}>Última 1h</option>
                    <option value={6}>Últimas 6h</option>
                    <option value={24}>Últimas 24h</option>
                    <option value={72}>Últimos 3 dias</option>
                    <option value={168}>Última semana</option>
                </select>
                <button onClick={carregar} className="flex items-center gap-2 bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded">
                    <RefreshCw size={16} className={loading ? 'animate-spin' : ''} /> Atualizar
                </button>
                <div className="ml-auto flex gap-4 text-sm">
                    <span><strong>{total}</strong> turnos</span>
                    <span>JSON: <strong>{json}</strong></span>
                    <span className={fallback > 0 ? 'text-amber-600' : ''}>Fallback: <strong>{fallback}</strong></span>
                    <span>Latência média: <strong>{latMedia}ms</strong></span>
                    <span>Tokens: <strong>{tokensTotal.toLocaleString()}</strong></span>
                </div>
            </div>

            <div className="space-y-2">
                {turns.map(t => (
                    <div key={t.id} className={`border rounded-lg ${t.error || t.fallback_used ? 'border-amber-400 bg-amber-50 dark:bg-amber-900/20' : 'bg-white dark:bg-slate-900'}`}>
                        <button
                            onClick={() => toggle(t.id)}
                            className="w-full flex items-center gap-3 p-3 text-left"
                        >
                            {expandido[t.id] ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                            <span className="text-xs font-mono text-slate-500">{new Date(t.created_at).toLocaleString('pt-BR')}</span>
                            <span className="text-xs font-mono bg-slate-100 dark:bg-slate-800 px-2 py-0.5 rounded">{t.user_id}</span>
                            <span className="flex-1 truncate text-sm">{t.user_input}</span>
                            <span className="text-xs text-slate-400">{t.latency_ms}ms</span>
                            {t.tokens_in && <span className="text-xs text-slate-400">{(t.tokens_in + t.tokens_out)} tok</span>}
                            <span className={`text-xs px-2 py-0.5 rounded ${t.output_format === 'json' ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'}`}>
                                {t.output_format}
                            </span>
                            {t.fallback_used && <span className="text-xs px-2 py-0.5 rounded bg-amber-200 text-amber-800">fallback</span>}
                            {t.error && <span className="text-xs px-2 py-0.5 rounded bg-red-200 text-red-800">erro</span>}
                        </button>
                        {expandido[t.id] && (
                            <div className="px-3 pb-3 space-y-3 border-t pt-3">
                                {t.error && (
                                    <div>
                                        <h4 className="text-xs font-semibold text-red-600 mb-1">Erro</h4>
                                        <pre className="text-xs bg-red-50 dark:bg-red-900/30 p-2 rounded whitespace-pre-wrap">{t.error}</pre>
                                    </div>
                                )}
                                <div>
                                    <h4 className="text-xs font-semibold text-slate-500 mb-1">Resposta final</h4>
                                    <pre className="text-xs bg-slate-50 dark:bg-slate-800 p-2 rounded whitespace-pre-wrap">{t.final_output}</pre>
                                </div>
                                <div>
                                    <h4 className="text-xs font-semibold text-slate-500 mb-1">Tool calls ({(t.tool_calls || []).length})</h4>
                                    <pre className="text-xs bg-slate-50 dark:bg-slate-800 p-2 rounded overflow-x-auto">{JSON.stringify(t.tool_calls, null, 2)}</pre>
                                </div>
                            </div>
                        )}
                    </div>
                ))}
                {!loading && turns.length === 0 && (
                    <p className="text-slate-400 text-sm text-center py-8">Nenhum turn no período/filtro.</p>
                )}
            </div>
        </div>
    )
}
