import { useState, useEffect, useRef } from 'react'
import { supabase } from './lib/supabase'
import { Activity, Save, RefreshCw, Power, GripVertical, Plus, Trash2, ChevronDown, ChevronRight, Puzzle } from 'lucide-react'

// ==================== HELPERS ====================

function parsePromptToBlocks(text) {
    if (!text || !text.trim()) return []

    const lines = text.split('\n')
    const blocks = []
    let currentBlock = null

    for (const line of lines) {
        const headerMatch = line.match(/^#\s+(.+)/)
        if (headerMatch) {
            if (currentBlock) blocks.push(currentBlock)
            currentBlock = {
                id: crypto.randomUUID(),
                title: headerMatch[1].trim(),
                content: '',
                collapsed: false
            }
        } else {
            if (!currentBlock) {
                // Text before first # — intro block
                currentBlock = {
                    id: crypto.randomUUID(),
                    title: 'INTRODUÇÃO',
                    content: '',
                    collapsed: false,
                    isIntro: true
                }
            }
            currentBlock.content += (currentBlock.content ? '\n' : '') + line
        }
    }
    if (currentBlock) blocks.push(currentBlock)

    // Trim content
    blocks.forEach(b => { b.content = b.content.trim() })

    return blocks
}

function blocksToPrompt(blocks) {
    return blocks.map(b => {
        if (b.isIntro) return b.content
        return `# ${b.title}\n${b.content}`
    }).join('\n\n')
}

// ==================== BLOCK CARD ====================

function PromptBlock({ block, index, onUpdate, onDelete, onDragStart, onDragOver, onDrop, isDragTarget }) {
    const [editing, setEditing] = useState(false)
    const [collapsed, setCollapsed] = useState(block.collapsed)

    return (
        <div
            draggable
            onDragStart={(e) => onDragStart(e, index)}
            onDragOver={(e) => onDragOver(e, index)}
            onDrop={(e) => onDrop(e, index)}
            className={`group bg-slate-900/70 border rounded-xl transition-all duration-200 ${isDragTarget
                    ? 'border-purple-500 shadow-lg shadow-purple-500/10 scale-[1.01]'
                    : 'border-slate-700/50 hover:border-slate-600'
                }`}
        >
            {/* HEADER */}
            <div className="flex items-center gap-2 px-4 py-3 cursor-grab active:cursor-grabbing">
                <GripVertical size={16} className="text-slate-600 flex-shrink-0" />

                <button
                    onClick={() => setCollapsed(!collapsed)}
                    className="text-slate-500 hover:text-slate-300 transition-colors flex-shrink-0"
                >
                    {collapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
                </button>

                {editing ? (
                    <input
                        type="text"
                        value={block.title}
                        onChange={(e) => onUpdate(index, { ...block, title: e.target.value })}
                        onBlur={() => setEditing(false)}
                        onKeyDown={(e) => e.key === 'Enter' && setEditing(false)}
                        autoFocus
                        className="flex-1 bg-transparent border-b border-purple-500 text-purple-300 text-sm font-semibold uppercase tracking-wide focus:outline-none"
                    />
                ) : (
                    <span
                        onDoubleClick={() => setEditing(true)}
                        className="flex-1 text-purple-300 text-sm font-semibold uppercase tracking-wide cursor-text"
                        title="Clique duas vezes para editar o título"
                    >
                        {block.title}
                    </span>
                )}

                {!block.isIntro && (
                    <button
                        onClick={() => onDelete(index)}
                        className="opacity-0 group-hover:opacity-100 text-slate-600 hover:text-red-400 transition-all p-1 rounded"
                        title="Remover bloco"
                    >
                        <Trash2 size={14} />
                    </button>
                )}
            </div>

            {/* CONTENT */}
            {!collapsed && (
                <div className="px-4 pb-4">
                    <textarea
                        value={block.content}
                        onChange={(e) => onUpdate(index, { ...block, content: e.target.value })}
                        className="w-full bg-slate-950/50 border border-slate-700/50 rounded-lg p-3 font-mono text-xs text-slate-300 focus:outline-none focus:border-purple-500/50 focus:ring-1 focus:ring-purple-500/30 transition-all resize-none leading-relaxed"
                        rows={Math.max(3, block.content.split('\n').length + 1)}
                        placeholder="Conteúdo do bloco..."
                    />
                </div>
            )}
        </div>
    )
}

// ==================== MAIN DASHBOARD ====================

export default function Dashboard() {
    const [config, setConfig] = useState(null)
    const [loading, setLoading] = useState(true)
    const [saving, setSaving] = useState(false)
    const [logs, setLogs] = useState([])
    const [blocks, setBlocks] = useState([])
    const [dragIndex, setDragIndex] = useState(null)
    const [dragOverIndex, setDragOverIndex] = useState(null)
    const [saveSuccess, setSaveSuccess] = useState(false)

    useEffect(() => {
        fetchConfig()
        fetchLogs()

        const channel = supabase
            .channel('public:chat_history')
            .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'chat_history' }, payload => {
                setLogs(current => [payload.new, ...current].slice(0, 50))
            })
            .subscribe()

        return () => { supabase.removeChannel(channel) }
    }, [])

    async function fetchConfig() {
        try {
            setLoading(true)
            const { data, error } = await supabase
                .from('bot_settings')
                .select('*')
                .eq('id', 1)
                .single()

            if (error) throw error
            if (data) {
                setConfig(data)
                setBlocks(parsePromptToBlocks(data.system_prompt || ""))
            }
        } catch (error) {
            console.error('Error fetching config:', error)
        } finally {
            setLoading(false)
        }
    }

    async function fetchLogs() {
        const { data } = await supabase
            .from('chat_history')
            .select('*')
            .order('created_at', { ascending: false })
            .limit(20)
        if (data) setLogs(data)
    }

    async function toggleBot() {
        if (!config) return
        const newState = !config.is_active
        try {
            const { error } = await supabase
                .from('bot_settings')
                .update({ is_active: newState, updated_at: new Date() })
                .eq('id', 1)
            if (error) throw error
            setConfig({ ...config, is_active: newState })
        } catch (error) {
            alert('Erro ao atualizar status: ' + error.message)
        }
    }

    async function savePrompt() {
        setSaving(true)
        try {
            const fullPrompt = blocksToPrompt(blocks)
            const { error } = await supabase
                .from('bot_settings')
                .update({ system_prompt: fullPrompt, updated_at: new Date() })
                .eq('id', 1)
            if (error) throw error
            setSaveSuccess(true)
            setTimeout(() => setSaveSuccess(false), 2000)
        } catch (error) {
            alert('Erro ao salvar prompt: ' + error.message)
        } finally {
            setSaving(false)
        }
    }

    // ===== DRAG AND DROP =====
    function handleDragStart(e, index) {
        setDragIndex(index)
        e.dataTransfer.effectAllowed = 'move'
    }

    function handleDragOver(e, index) {
        e.preventDefault()
        setDragOverIndex(index)
    }

    function handleDrop(e, dropIndex) {
        e.preventDefault()
        if (dragIndex === null || dragIndex === dropIndex) {
            setDragIndex(null)
            setDragOverIndex(null)
            return
        }
        const updated = [...blocks]
        const [moved] = updated.splice(dragIndex, 1)
        updated.splice(dropIndex, 0, moved)
        setBlocks(updated)
        setDragIndex(null)
        setDragOverIndex(null)
    }

    function updateBlock(index, newBlock) {
        const updated = [...blocks]
        updated[index] = newBlock
        setBlocks(updated)
    }

    function deleteBlock(index) {
        setBlocks(blocks.filter((_, i) => i !== index))
    }

    function addBlock() {
        setBlocks([...blocks, {
            id: crypto.randomUUID(),
            title: 'NOVO BLOCO',
            content: '',
            collapsed: false
        }])
    }

    if (loading) return <div className="min-h-screen flex items-center justify-center text-white">Carregando...</div>

    return (
        <div className="min-h-screen bg-slate-950 p-4 md:p-8 text-slate-100 font-sans">
            <div className="max-w-6xl mx-auto space-y-6">

                {/* HEADER */}
                <header className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 bg-slate-900/50 p-6 rounded-2xl border border-slate-800 backdrop-blur-md">
                    <div>
                        <h1 className="text-2xl md:text-3xl font-bold bg-gradient-to-r from-blue-400 to-purple-500 bg-clip-text text-transparent">
                            Bot Control Center
                        </h1>
                        <p className="text-slate-400 text-sm">Gerencie a inteligência da sua operação</p>
                    </div>

                    <div className="flex items-center gap-3">
                        <div className={`flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium transition-all ${config?.is_active ? 'bg-green-500/10 text-green-400 border border-green-500/20' : 'bg-red-500/10 text-red-400 border border-red-500/20'}`}>
                            <Activity size={16} />
                            {config?.is_active ? 'ONLINE' : 'OFFLINE'}
                        </div>
                        <button
                            onClick={toggleBot}
                            className={`p-3 rounded-xl transition-all ${config?.is_active ? 'bg-slate-800 hover:bg-red-900/30 text-slate-300 hover:text-red-400' : 'bg-green-600 hover:bg-green-500 text-white shadow-lg shadow-green-900/20'}`}
                            title={config?.is_active ? "Desligar Bot" : "Ligar Bot"}
                        >
                            <Power size={22} />
                        </button>
                    </div>
                </header>

                <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

                    {/* LEFT — PROMPT BUILDER */}
                    <div className="lg:col-span-2 space-y-4">
                        <div className="bg-slate-900/50 rounded-2xl border border-slate-800 p-5 backdrop-blur-md">
                            {/* Builder Header */}
                            <div className="flex justify-between items-center mb-5">
                                <div className="flex items-center gap-2 text-purple-400">
                                    <Puzzle size={20} />
                                    <h2 className="font-semibold text-lg">Prompt Builder</h2>
                                    <span className="text-xs text-slate-500 ml-2">{blocks.length} blocos</span>
                                </div>
                                <button
                                    onClick={savePrompt}
                                    disabled={saving}
                                    className={`flex items-center gap-2 px-4 py-2 rounded-lg transition-all text-sm font-medium disabled:opacity-50 ${saveSuccess
                                            ? 'bg-green-600 text-white'
                                            : 'bg-purple-600 hover:bg-purple-500 text-white'
                                        }`}
                                >
                                    <Save size={16} />
                                    {saving ? 'Salvando...' : saveSuccess ? 'Salvo ✓' : 'Salvar'}
                                </button>
                            </div>

                            {/* Blocks */}
                            <div
                                className="space-y-3"
                                onDragEnd={() => { setDragIndex(null); setDragOverIndex(null) }}
                            >
                                {blocks.map((block, index) => (
                                    <PromptBlock
                                        key={block.id}
                                        block={block}
                                        index={index}
                                        onUpdate={updateBlock}
                                        onDelete={deleteBlock}
                                        onDragStart={handleDragStart}
                                        onDragOver={handleDragOver}
                                        onDrop={handleDrop}
                                        isDragTarget={dragOverIndex === index && dragIndex !== index}
                                    />
                                ))}
                            </div>

                            {/* Add Block Button */}
                            <button
                                onClick={addBlock}
                                className="w-full mt-4 py-3 border-2 border-dashed border-slate-700 hover:border-purple-500/50 rounded-xl text-slate-500 hover:text-purple-400 transition-all flex items-center justify-center gap-2 text-sm"
                            >
                                <Plus size={18} />
                                Adicionar Bloco
                            </button>
                        </div>
                    </div>

                    {/* RIGHT — LOGS */}
                    <div className="space-y-4">
                        <div className="bg-slate-900/50 rounded-2xl border border-slate-800 p-5 backdrop-blur-md">
                            <div className="flex justify-between items-center mb-4">
                                <h2 className="font-semibold text-lg text-blue-400">Live Logs</h2>
                                <button onClick={fetchLogs} className="p-2 hover:bg-slate-800 rounded-lg text-slate-400 transition-colors">
                                    <RefreshCw size={16} />
                                </button>
                            </div>

                            <div className="space-y-2 max-h-[600px] overflow-y-auto pr-1">
                                {logs.length === 0 && <p className="text-slate-500 text-center py-10 text-sm">Nenhum log recente.</p>}

                                {logs.map((log) => (
                                    <div key={log.id} className="p-3 bg-slate-950/50 border border-slate-800 rounded-xl text-xs space-y-1.5">
                                        <div className="flex justify-between items-start text-slate-500">
                                            <span>{new Date(log.created_at).toLocaleTimeString()}</span>
                                            <span className={`px-2 py-0.5 rounded-full ${log.role === 'user' ? 'bg-blue-500/10 text-blue-400' : 'bg-green-500/10 text-green-400'}`}>
                                                {log.role}
                                            </span>
                                        </div>
                                        <p className="text-slate-300 break-words leading-relaxed">{log.content}</p>
                                        <div className="text-[10px] text-slate-600 text-right font-mono">ID: {log.user_id}</div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>

                </div>
            </div>
        </div>
    )
}
