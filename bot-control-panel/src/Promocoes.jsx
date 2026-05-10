import { useState, useEffect, useMemo } from 'react'
import { supabase } from './lib/supabase'
import { toast } from 'sonner'
import { Trash2, Save, Plus, X, Edit2 } from 'lucide-react'

const OUTRO_VALUE = '__OUTRO__'

export default function Promocoes() {
    const [periodos, setPeriodos] = useState([])
    const [categorias, setCategorias] = useState([])
    const [loading, setLoading] = useState(true)
    const [editingId, setEditingId] = useState(null)  // id em edição (ou 'new' pra novo)

    async function carregar() {
        setLoading(true)
        try {
            const [perRes, catRes] = await Promise.all([
                supabase.from('dia_s_calendario').select('*').order('data_inicio', { ascending: false }),
                supabase
                    .from('produtos_estoque')
                    .select('nome_grupo')
                    .gt('estoque', 0)
                    .not('nome_grupo', 'is', null),
            ])
            setPeriodos(perRes.data || [])

            // Distinct nome_grupo + count, ordenado por count desc
            const counts = {}
            for (const row of (catRes.data || [])) {
                const g = row.nome_grupo
                if (!g) continue
                counts[g] = (counts[g] || 0) + 1
            }
            const lista = Object.entries(counts)
                .filter(([g]) => g !== 'PRODUTOS MAIS VENDIDOS')  // meta-grupo não é categoria
                .sort((a, b) => b[1] - a[1])
                .map(([g, n]) => ({ nome: g, qtd: n }))
            setCategorias(lista)
        } catch (e) {
            toast.error('Erro ao carregar: ' + e.message)
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => { carregar() }, [])

    function startEdit(periodo) {
        setEditingId(periodo ? periodo.id : 'new')
    }

    function cancelEdit() {
        setEditingId(null)
    }

    async function salvar(payload, id) {
        try {
            const isNew = !id || id === 'new'
            const data = {
                data_inicio: payload.data_inicio,
                data_fim: payload.data_fim,
                categoria: payload.categoria,
                percentual: payload.percentual,
                observacao: payload.observacao || null,
            }
            const { error } = isNew
                ? await supabase.from('dia_s_calendario').insert(data)
                : await supabase.from('dia_s_calendario').update(data).eq('id', id)
            if (error) throw error
            toast.success(isNew ? 'Período cadastrado' : 'Período atualizado')
            setEditingId(null)
            carregar()
        } catch (e) {
            toast.error('Erro: ' + e.message)
        }
    }

    async function remover(id) {
        if (!confirm('Apagar este período?')) return
        try {
            const { error } = await supabase.from('dia_s_calendario').delete().eq('id', id)
            if (error) throw error
            toast.success('Período removido')
            carregar()
        } catch (e) {
            toast.error('Erro: ' + e.message)
        }
    }

    if (loading) return <div className="p-6 text-slate-500">Carregando…</div>

    return (
        <div className="p-6 space-y-6 max-w-4xl">
            <div>
                <h2 className="text-lg font-semibold mb-1">Calendário Dia S</h2>
                <p className="text-sm text-slate-500">
                    Cadastre os períodos em que cada categoria estará no Dia S (20% off em PIX/dinheiro, sem troca).
                    O bot consulta esta tabela toda vez que cliente pergunta sobre promoção.
                </p>
            </div>

            {editingId === 'new' ? (
                <PeriodoForm
                    categorias={categorias}
                    onSalvar={(p) => salvar(p, null)}
                    onCancelar={cancelEdit}
                />
            ) : (
                <button
                    onClick={() => startEdit(null)}
                    className="flex items-center gap-2 bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded">
                    <Plus size={16} /> Novo período
                </button>
            )}

            <div>
                <h3 className="text-sm font-semibold text-slate-600 dark:text-slate-300 mb-2">
                    Períodos cadastrados ({periodos.length})
                </h3>
                {periodos.length === 0 ? (
                    <p className="text-slate-400 text-sm">Nenhum período cadastrado ainda.</p>
                ) : (
                    <div className="space-y-2">
                        {periodos.map(p => (
                            editingId === p.id ? (
                                <PeriodoForm
                                    key={p.id}
                                    inicial={p}
                                    categorias={categorias}
                                    onSalvar={(payload) => salvar(payload, p.id)}
                                    onCancelar={cancelEdit}
                                />
                            ) : (
                                <PeriodoRow
                                    key={p.id}
                                    periodo={p}
                                    onEditar={() => startEdit(p)}
                                    onRemover={() => remover(p.id)}
                                />
                            )
                        ))}
                    </div>
                )}
            </div>
        </div>
    )
}


function PeriodoRow({ periodo, onEditar, onRemover }) {
    const fmtData = (s) => {
        if (!s) return ''
        const [y, m, d] = s.split('-')
        return `${d}/${m}/${y}`
    }
    return (
        <div className="border rounded-lg p-3 flex items-center gap-3 bg-white dark:bg-slate-900">
            <div className="text-sm">
                <strong>{fmtData(periodo.data_inicio)}</strong>
                {periodo.data_fim !== periodo.data_inicio && (
                    <> a <strong>{fmtData(periodo.data_fim)}</strong></>
                )}
            </div>
            <span className="px-2 py-1 bg-purple-100 dark:bg-purple-900 text-purple-700 dark:text-purple-200 rounded text-xs font-medium">
                {periodo.categoria}
            </span>
            <span className="text-sm text-slate-600 dark:text-slate-400">{periodo.percentual}% off</span>
            {periodo.observacao && (
                <span className="text-xs text-slate-400 italic truncate max-w-xs">{periodo.observacao}</span>
            )}
            <button onClick={onEditar} className="ml-auto text-slate-500 hover:text-purple-600 p-1">
                <Edit2 size={16} />
            </button>
            <button onClick={onRemover} className="text-red-500 hover:text-red-700 p-1">
                <Trash2 size={16} />
            </button>
        </div>
    )
}


function PeriodoForm({ inicial = null, categorias, onSalvar, onCancelar }) {
    const isEdit = !!inicial
    const isCategoriaCustom = useMemo(() => {
        if (!inicial?.categoria) return false
        return !categorias.some(c => c.nome === inicial.categoria)
    }, [inicial, categorias])

    const [dataInicio, setDataInicio] = useState(inicial?.data_inicio || '')
    const [dataFim, setDataFim] = useState(inicial?.data_fim || '')
    const [categoriaSelect, setCategoriaSelect] = useState(
        isCategoriaCustom ? OUTRO_VALUE : (inicial?.categoria || '')
    )
    const [categoriaCustom, setCategoriaCustom] = useState(
        isCategoriaCustom ? inicial.categoria : ''
    )
    const [percentual, setPercentual] = useState(inicial?.percentual ?? 20)
    const [observacao, setObservacao] = useState(inicial?.observacao || '')

    function handleSubmit() {
        if (!dataInicio) return toast.error('Informe a data de início')
        const final = dataFim || dataInicio
        const categoriaFinal = categoriaSelect === OUTRO_VALUE
            ? (categoriaCustom || '').trim()
            : categoriaSelect
        if (!categoriaFinal) return toast.error('Selecione ou digite uma categoria')
        if (!percentual || percentual < 0 || percentual > 100) return toast.error('Desconto entre 0 e 100')

        onSalvar({
            data_inicio: dataInicio,
            data_fim: final,
            categoria: categoriaFinal,
            percentual,
            observacao,
        })
    }

    return (
        <div className="border-2 border-purple-300 dark:border-purple-700 rounded-lg p-5 bg-purple-50 dark:bg-slate-800 space-y-4">
            <h3 className="font-semibold text-purple-900 dark:text-purple-200">
                {isEdit ? `Editar período #${inicial.id}` : 'Novo período de Dia S'}
            </h3>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                    <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
                        Data de início <span className="text-red-500">*</span>
                    </label>
                    <input type="date" value={dataInicio} onChange={e => setDataInicio(e.target.value)}
                        className="w-full border rounded px-3 py-2 dark:bg-slate-700 dark:border-slate-600" />
                </div>
                <div>
                    <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
                        Data de fim <span className="text-slate-400">(vazio = mesmo dia do início)</span>
                    </label>
                    <input type="date" value={dataFim} onChange={e => setDataFim(e.target.value)}
                        className="w-full border rounded px-3 py-2 dark:bg-slate-700 dark:border-slate-600" />
                </div>
            </div>

            <div>
                <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
                    Categoria <span className="text-red-500">*</span>
                    <span className="text-slate-400 font-normal ml-1">(grupos do estoque)</span>
                </label>
                <select value={categoriaSelect} onChange={e => setCategoriaSelect(e.target.value)}
                    className="w-full border rounded px-3 py-2 dark:bg-slate-700 dark:border-slate-600">
                    <option value="">— escolha —</option>
                    {categorias.map(c => (
                        <option key={c.nome} value={c.nome}>
                            {c.nome} ({c.qtd} produtos)
                        </option>
                    ))}
                    <option value={OUTRO_VALUE}>Outro (digitar manualmente)</option>
                </select>
                {categoriaSelect === OUTRO_VALUE && (
                    <input
                        type="text"
                        placeholder="Digite a categoria personalizada"
                        value={categoriaCustom}
                        onChange={e => setCategoriaCustom(e.target.value)}
                        className="w-full border rounded px-3 py-2 dark:bg-slate-700 dark:border-slate-600 mt-2" />
                )}
            </div>

            <div>
                <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
                    Desconto (%) <span className="text-red-500">*</span>
                </label>
                <input type="number" min="0" max="100" value={percentual}
                    onChange={e => setPercentual(parseInt(e.target.value) || 0)}
                    className="w-full md:w-32 border rounded px-3 py-2 dark:bg-slate-700 dark:border-slate-600" />
            </div>

            <div>
                <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
                    Observação <span className="text-slate-400 font-normal">(opcional)</span>
                </label>
                <textarea value={observacao} onChange={e => setObservacao(e.target.value)} rows={2}
                    placeholder="Ex: válido só na loja matriz; não combina com outras promoções"
                    className="w-full border rounded px-3 py-2 dark:bg-slate-700 dark:border-slate-600" />
            </div>

            <div className="flex gap-2">
                <button onClick={handleSubmit}
                    className="bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded flex items-center gap-2">
                    <Save size={16} /> {isEdit ? 'Salvar alterações' : 'Adicionar período'}
                </button>
                <button onClick={onCancelar}
                    className="bg-slate-200 hover:bg-slate-300 dark:bg-slate-700 dark:hover:bg-slate-600 px-4 py-2 rounded flex items-center gap-2">
                    <X size={16} /> Cancelar
                </button>
            </div>
        </div>
    )
}
