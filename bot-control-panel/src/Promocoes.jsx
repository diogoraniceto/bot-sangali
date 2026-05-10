import { useState, useEffect } from 'react'
import { supabase } from './lib/supabase'
import { toast } from 'sonner'
import { Trash2, Save, Plus } from 'lucide-react'

const DIAS_SEMANA = ['Domingo', 'Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado']

export default function Promocoes() {
    const [semanais, setSemanais] = useState([])
    const [calendario, setCalendario] = useState([])
    const [loading, setLoading] = useState(true)
    const [tab, setTab] = useState('semanais')

    async function carregar() {
        setLoading(true)
        try {
            const [s, c] = await Promise.all([
                supabase.from('promocoes_ativas').select('*').order('id'),
                supabase.from('dia_s_calendario').select('*').order('data_inicio', { ascending: false }),
            ])
            setSemanais(s.data || [])
            setCalendario(c.data || [])
        } catch (e) {
            toast.error('Erro ao carregar promoções: ' + e.message)
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => { carregar() }, [])

    async function salvarSemanal(row) {
        try {
            const { error } = await supabase.from('promocoes_ativas').update({
                categoria: row.categoria,
                percentual: row.percentual,
                ativa: row.ativa,
                observacao: row.observacao,
                updated_at: new Date().toISOString(),
            }).eq('id', row.id)
            if (error) throw error
            toast.success('Promoção semanal atualizada')
            carregar()
        } catch (e) {
            toast.error('Erro: ' + e.message)
        }
    }

    async function salvarCalendario(row) {
        try {
            const isNew = !row.id
            const payload = {
                data_inicio: row.data_inicio,
                data_fim: row.data_fim,
                categoria: row.categoria,
                percentual: row.percentual,
                observacao: row.observacao,
            }
            const { error } = isNew
                ? await supabase.from('dia_s_calendario').insert(payload)
                : await supabase.from('dia_s_calendario').update(payload).eq('id', row.id)
            if (error) throw error
            toast.success(isNew ? 'Período cadastrado' : 'Período atualizado')
            carregar()
        } catch (e) {
            toast.error('Erro: ' + e.message)
        }
    }

    async function removerCalendario(id) {
        if (!confirm('Apagar este período?')) return
        try {
            const { error } = await supabase.from('dia_s_calendario').delete().eq('id', id)
            if (error) throw error
            toast.success('Removido')
            carregar()
        } catch (e) {
            toast.error('Erro: ' + e.message)
        }
    }

    if (loading) return <div className="p-6">Carregando…</div>

    return (
        <div className="p-6 space-y-6">
            <div className="flex gap-2 border-b">
                <button onClick={() => setTab('semanais')}
                    className={`px-4 py-2 ${tab === 'semanais' ? 'border-b-2 border-purple-600 font-semibold' : 'text-slate-500'}`}>
                    Promoções semanais
                </button>
                <button onClick={() => setTab('calendario')}
                    className={`px-4 py-2 ${tab === 'calendario' ? 'border-b-2 border-purple-600 font-semibold' : 'text-slate-500'}`}>
                    Calendário Dia S
                </button>
            </div>

            {tab === 'semanais' && (
                <div className="space-y-4">
                    <p className="text-sm text-slate-500">Promoções recorrentes por dia da semana. O bot consulta isso quando cliente pergunta sobre promoção.</p>
                    {semanais.map(row => (
                        <div key={row.id} className="border rounded-lg p-4 space-y-3 bg-white dark:bg-slate-900">
                            <div className="flex items-center gap-3">
                                <span className="text-lg font-semibold">{row.nome}</span>
                                <span className="text-sm text-slate-500">{DIAS_SEMANA[row.dia_semana]}</span>
                                <label className="ml-auto flex items-center gap-2 text-sm">
                                    <input type="checkbox" checked={row.ativa}
                                        onChange={e => setSemanais(s => s.map(r => r.id === row.id ? { ...r, ativa: e.target.checked } : r))} />
                                    Ativa
                                </label>
                            </div>
                            <div className="grid grid-cols-2 gap-3">
                                <input type="text" placeholder="Categoria" value={row.categoria || ''}
                                    onChange={e => setSemanais(s => s.map(r => r.id === row.id ? { ...r, categoria: e.target.value } : r))}
                                    className="border rounded px-3 py-2 dark:bg-slate-800" />
                                <input type="number" placeholder="Desconto %" value={row.percentual || 0}
                                    onChange={e => setSemanais(s => s.map(r => r.id === row.id ? { ...r, percentual: parseInt(e.target.value) } : r))}
                                    className="border rounded px-3 py-2 dark:bg-slate-800" />
                            </div>
                            <textarea placeholder="Observação" value={row.observacao || ''}
                                onChange={e => setSemanais(s => s.map(r => r.id === row.id ? { ...r, observacao: e.target.value } : r))}
                                className="border rounded px-3 py-2 w-full dark:bg-slate-800" rows={2} />
                            <button onClick={() => salvarSemanal(row)}
                                className="bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded flex items-center gap-2">
                                <Save size={16} /> Salvar
                            </button>
                        </div>
                    ))}
                </div>
            )}

            {tab === 'calendario' && (
                <CalendarioTab calendario={calendario} onSalvar={salvarCalendario} onRemover={removerCalendario} />
            )}
        </div>
    )
}

function CalendarioTab({ calendario, onSalvar, onRemover }) {
    const [novo, setNovo] = useState({ data_inicio: '', data_fim: '', categoria: '', percentual: 20, observacao: '' })
    return (
        <div className="space-y-4">
            <p className="text-sm text-slate-500">Períodos com categoria específica. Sobrescreve as semanais quando coincide.</p>

            <div className="border rounded-lg p-4 bg-purple-50 dark:bg-slate-800 space-y-3">
                <h3 className="font-semibold flex items-center gap-2"><Plus size={16} /> Novo período</h3>
                <div className="grid grid-cols-2 gap-3">
                    <input type="date" value={novo.data_inicio} onChange={e => setNovo({ ...novo, data_inicio: e.target.value })} className="border rounded px-3 py-2 dark:bg-slate-700" />
                    <input type="date" value={novo.data_fim} onChange={e => setNovo({ ...novo, data_fim: e.target.value })} className="border rounded px-3 py-2 dark:bg-slate-700" />
                </div>
                <input type="text" placeholder="Categoria (ex: lingerie básica)" value={novo.categoria} onChange={e => setNovo({ ...novo, categoria: e.target.value })} className="border rounded px-3 py-2 w-full dark:bg-slate-700" />
                <input type="number" placeholder="Desconto %" value={novo.percentual} onChange={e => setNovo({ ...novo, percentual: parseInt(e.target.value) })} className="border rounded px-3 py-2 w-full dark:bg-slate-700" />
                <button onClick={() => { onSalvar(novo); setNovo({ data_inicio: '', data_fim: '', categoria: '', percentual: 20, observacao: '' }) }}
                    disabled={!novo.data_inicio || !novo.categoria}
                    className="bg-purple-600 hover:bg-purple-700 disabled:opacity-50 text-white px-4 py-2 rounded">
                    Adicionar período
                </button>
            </div>

            <div className="space-y-2">
                {calendario.map(row => (
                    <div key={row.id} className="border rounded-lg p-3 flex items-center gap-3">
                        <div className="text-sm">
                            <strong>{row.data_inicio}</strong> a <strong>{row.data_fim}</strong>
                        </div>
                        <span className="px-2 py-1 bg-purple-100 dark:bg-purple-900 rounded text-xs">{row.categoria}</span>
                        <span className="text-sm">{row.percentual}% off</span>
                        <button onClick={() => onRemover(row.id)} className="ml-auto text-red-500 hover:text-red-700">
                            <Trash2 size={16} />
                        </button>
                    </div>
                ))}
                {calendario.length === 0 && <p className="text-slate-400 text-sm">Nenhum período cadastrado.</p>}
            </div>
        </div>
    )
}
