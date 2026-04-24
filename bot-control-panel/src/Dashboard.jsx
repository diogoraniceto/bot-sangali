import { useState, useEffect, useRef, useMemo } from 'react'
import { supabase } from './lib/supabase'
import { toast, Toaster } from 'sonner'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, ResponsiveContainer } from 'recharts'
import {
    Bell, Search, LayoutDashboard, MessageSquare, Box, Puzzle, RotateCcw, PanelLeft, Bot, Zap, Plus, Settings, Play, Link, Download, Move, LogOut, FileText, Sun, Moon, LayoutGrid, List, ImagePlus, Filter, UploadCloud, Activity, RefreshCw, AlertCircle, TrendingUp, ShoppingCart, Users, Check, X, Camera, Image as ImageIcon,
    GripVertical, Sparkles, ChevronDown, ChevronRight, Trash2, Save, MessageCircle, UserCircle2, Package, ArrowUpDown, ArrowUp, ArrowDown,
    Command, Plug
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
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
                id: Math.random().toString(36).substring(2, 11),
                title: headerMatch[1].trim(),
                content: '',
                collapsed: false
            }
        } else {
            if (!currentBlock) {
                currentBlock = {
                    id: Math.random().toString(36).substring(2, 11),
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
    blocks.forEach(b => { b.content = b.content.trim() })
    return blocks
}

function blocksToPrompt(blocks) {
    return blocks.map(b => {
        if (b.isIntro) return b.content
        return `# ${b.title}\n${b.content}`
    }).join('\n\n')
}

// ==================== COMPONENTS ====================

function SidebarItem({ icon: Icon, label, active, onClick }) {
    return (
        <button
            onClick={onClick}
            className={`w-full flex flex-col items-center justify-center gap-1.5 px-2 py-3 rounded-lg transition-colors font-medium group ${active
                ? 'bg-purple-50 dark:bg-[#1E1430] text-purple-600 dark:text-purple-100'
                : 'text-slate-500 hover:text-slate-800 hover:bg-slate-100 dark:text-[#A1A1AA] dark:hover:bg-[#1A1A1A] dark:hover:text-slate-200'
                }`}
        >
            <Icon size={20} className={active ? 'text-purple-600 dark:text-purple-400' : 'text-slate-400 dark:text-[#71717A] group-hover:text-slate-600 dark:group-hover:text-[#A1A1AA]'} strokeWidth={active ? 2.5 : 2} />
            <span className="text-[10px] leading-tight text-center tracking-wide">{label}</span>
        </button>
    )
}

function PromptBlock({ block, index, onUpdate, onDelete, onDragStart, onDragOver, onDrop, isDragTarget }) {
    const [editing, setEditing] = useState(false)
    const [collapsed, setCollapsed] = useState(block.collapsed)

    return (
        <div
            draggable
            onDragStart={(e) => onDragStart(e, index)}
            onDragOver={(e) => onDragOver(e, index)}
            onDrop={(e) => onDrop(e, index)}
            className={`group bg-white dark:bg-[#111111] border rounded-xl overflow-hidden transition-all duration-200 ${isDragTarget
                ? 'border-purple-500 scale-[1.01] shadow-lg shadow-purple-500/10'
                : 'border-slate-200 dark:border-[#1E1E1E] hover:border-slate-300 dark:hover:border-[#333] shadow-sm'
                }`}
        >
            {/* HEADER INTERNO */}
            <div className="flex items-center gap-2 px-4 py-3 bg-slate-50 dark:bg-[#0A0A0A] cursor-grab active:cursor-grabbing border-b border-slate-200 dark:border-[#1E1E1E]">
                <GripVertical size={14} className="text-slate-400 dark:text-[#71717A] flex-shrink-0" />

                <button
                    onClick={(e) => { e.stopPropagation(); setCollapsed(!collapsed); }}
                    className="text-slate-500 dark:text-[#A1A1AA] hover:text-slate-800 dark:hover:text-slate-200 transition-colors p-1 rounded hover:bg-slate-200 dark:hover:bg-[#1E1E1E] cursor-pointer"
                >
                    {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                </button>

                <Sparkles size={14} className="text-purple-500 opacity-90" />

                {editing ? (
                    <input
                        type="text"
                        value={block.title}
                        onChange={(e) => onUpdate(index, { ...block, title: e.target.value })}
                        onBlur={() => setEditing(false)}
                        onKeyDown={(e) => e.key === 'Enter' && setEditing(false)}
                        autoFocus
                        className="flex-1 bg-transparent border-b border-purple-500 text-slate-800 dark:text-slate-100 text-sm font-semibold tracking-wide focus:outline-none"
                    />
                ) : (
                    <span
                        onDoubleClick={(e) => { e.stopPropagation(); setEditing(true); }}
                        className="flex-1 text-slate-700 dark:text-slate-200 text-sm font-semibold tracking-wide cursor-text truncate"
                        title="Clique duas vezes para editar o título"
                    >
                        {block.title}
                    </span>
                )}

                {!block.isIntro && (
                    <button
                        onClick={(e) => { e.stopPropagation(); onDelete(index); }}
                        className="opacity-0 group-hover:opacity-100 text-slate-400 dark:text-[#71717A] hover:text-red-500 dark:hover:text-red-400 transition-colors duration-200 p-1.5 rounded hover:bg-slate-200 dark:hover:bg-[#1E1E1E] cursor-pointer"
                        title="Remover bloco"
                    >
                        <Trash2 size={14} />
                    </button>
                )}
            </div>

            {/* CONTENT */}
            {!collapsed && (
                <div className="bg-transparent">
                    <textarea
                        value={block.content}
                        onChange={(e) => onUpdate(index, { ...block, content: e.target.value })}
                        className="w-full bg-transparent border-none p-5 font-mono text-[13px] text-slate-600 dark:text-slate-300 focus:outline-none transition-all duration-200 resize-none leading-relaxed placeholder:text-slate-400 dark:placeholder:text-[#71717A] custom-scrollbar overflow-y-auto"
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
    const [theme, setTheme] = useState(localStorage.getItem('theme') || 'light')
    const [saving, setSaving] = useState(false)
    const [logs, setLogs] = useState([])
    const [blocks, setBlocks] = useState([])
    const [dragIndex, setDragIndex] = useState(null)
    const [dragOverIndex, setDragOverIndex] = useState(null)
    const [saveSuccess, setSaveSuccess] = useState(false)

    // UI State
    const [activeSidebarItem, setActiveSidebarItem] = useState('dashboard') // 'dashboard', 'inbox', 'comandos', 'estoque', 'integracoes', 'sincronizacao'
    const [selectedConversation, setSelectedConversation] = useState(null)
    const messagesEndRef = useRef(null)

    // Estoque State
    const ESTOQUE_PAGE_SIZE = 50
    const [estoque, setEstoque] = useState([])
    const [estoqueTotal, setEstoqueTotal] = useState(0)
    const [estoquePage, setEstoquePage] = useState(0)
    const [estoqueSearch, setEstoqueSearch] = useState('')
    const [estoqueSearchDebounced, setEstoqueSearchDebounced] = useState('')
    const [sortConfig, setSortConfig] = useState({ key: 'nome', direction: 'asc' })
    const [isFetchingEstoque, setIsFetchingEstoque] = useState(false)
    const [lastSyncDate, setLastSyncDate] = useState(null)
    const [viewMode, setViewMode] = useState('gallery') // 'table' | 'gallery'
    const [imageFilter, setImageFilter] = useState('all') // 'all' | 'missing' | 'present'
    const [lojaFilter, setLojaFilter] = useState('all')
    const [lojasList, setLojasList] = useState([])
    const [imagensMap, setImagensMap] = useState({})
    const [idsComImagem, setIdsComImagem] = useState([])
    const fileInputRef = useRef(null)
    const [uploadingProductId, setUploadingProductId] = useState(null)

    // Handoff / Atendente humano
    const [operatorNumber, setOperatorNumber] = useState('')
    const [savingOperator, setSavingOperator] = useState(false)
    const [operatorSaved, setOperatorSaved] = useState(false)

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

    useEffect(() => {
        if (theme === 'dark') {
            document.documentElement.classList.add('dark')
        } else {
            document.documentElement.classList.remove('dark')
        }
        localStorage.setItem('theme', theme)
    }, [theme])

    const toggleTheme = () => {
        setTheme(theme === 'light' ? 'dark' : 'light')
    }

    useEffect(() => {
        if (activeSidebarItem === 'inbox' && selectedConversation) {
            messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
        }
    }, [activeSidebarItem, selectedConversation, logs])

    // Debounce da busca para evitar uma query por tecla
    useEffect(() => {
        const t = setTimeout(() => setEstoqueSearchDebounced(estoqueSearch.trim()), 300)
        return () => clearTimeout(t)
    }, [estoqueSearch])

    // Resetar para a primeira página sempre que filtros/busca/sort mudarem
    useEffect(() => {
        setEstoquePage(0)
    }, [estoqueSearchDebounced, lojaFilter, imageFilter, sortConfig])

    // Carregar metadados leves (lojas, imagens, last_sync) ao entrar na aba Estoque
    useEffect(() => {
        if (activeSidebarItem !== 'estoque') return
        loadEstoqueMetadados()
    }, [activeSidebarItem])

    // (Re)carregar página atual quando filtros, sort, página ou metadados mudam
    useEffect(() => {
        if (activeSidebarItem !== 'estoque') return
        fetchEstoquePage()
    }, [activeSidebarItem, estoqueSearchDebounced, lojaFilter, imageFilter, sortConfig, estoquePage, idsComImagem])

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
                setOperatorNumber(data.operator_number || '')
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
            .select('id, user_id, role, content, created_at')
            .order('created_at', { ascending: false })
            .limit(50)
        if (data) setLogs(data)
    }

    // ============ CACHE (sessionStorage, TTL 5min) ============
    const ESTOQUE_CACHE_PREFIX = 'vx_estoque:v1'
    const ESTOQUE_CACHE_TTL_MS = 5 * 60 * 1000

    function cacheGet(key) {
        try {
            const raw = sessionStorage.getItem(`${ESTOQUE_CACHE_PREFIX}:${key}`)
            if (!raw) return null
            const { data, ts } = JSON.parse(raw)
            if (Date.now() - ts > ESTOQUE_CACHE_TTL_MS) return null
            return data
        } catch { return null }
    }
    function cacheSet(key, data) {
        try {
            sessionStorage.setItem(`${ESTOQUE_CACHE_PREFIX}:${key}`, JSON.stringify({ data, ts: Date.now() }))
        } catch {}
    }
    function cacheClearAll() {
        try {
            for (let i = sessionStorage.length - 1; i >= 0; i--) {
                const k = sessionStorage.key(i)
                if (k && k.startsWith(ESTOQUE_CACHE_PREFIX)) sessionStorage.removeItem(k)
            }
        } catch {}
    }

    // ============ METADADOS (lojas distintas, mapa de imagens, último sync) ============
    async function loadEstoqueMetadados({ useCache = true } = {}) {
        // Imagens (id_produto -> url). Tabela pequena (~3k linhas), busca uma vez por sessão.
        let imagens = useCache ? cacheGet('imagens') : null
        if (!imagens) {
            const { data } = await supabase
                .from('produtos_imagens')
                .select('produto_id, imagem_url')
                .range(0, 9999)
            imagens = {}
            if (data) {
                for (const row of data) {
                    if (row.produto_id && row.imagem_url && !imagens[row.produto_id]) {
                        imagens[row.produto_id] = row.imagem_url
                    }
                }
                cacheSet('imagens', imagens)
            }
        }
        setImagensMap(imagens)
        setIdsComImagem(Object.keys(imagens))

        // Lojas distintas
        let lojas = useCache ? cacheGet('lojas') : null
        if (!lojas) {
            const { data } = await supabase
                .from('produtos_estoque')
                .select('loja')
                .not('loja', 'is', null)
                .range(0, 9999)
            const set = new Set()
            if (data) for (const r of data) if (r.loja) set.add(r.loja)
            lojas = [...set].sort()
            cacheSet('lojas', lojas)
        }
        setLojasList(lojas)

        // Última sincronização
        let lastSync = useCache ? cacheGet('lastSync') : null
        if (!lastSync) {
            const { data } = await supabase
                .from('produtos_estoque')
                .select('last_sync')
                .order('last_sync', { ascending: false })
                .limit(1)
            lastSync = data && data[0]?.last_sync ? data[0].last_sync : null
            if (lastSync) cacheSet('lastSync', lastSync)
        }
        setLastSyncDate(lastSync ? new Date(lastSync) : null)
    }

    // ============ PAGINAÇÃO SERVER-SIDE ============
    // Colunas ordenáveis existentes em produtos_estoque (evita ordenar por coluna inexistente).
    const ESTOQUE_SORT_ALLOWED = new Set([
        'loja', 'id_produto', 'nome', 'tamanho', 'preco',
        'preco_varejo', 'preco_atacado', 'estoque', 'last_sync'
    ])

    function buildPageCacheKey() {
        return `page:${estoqueSearchDebounced}|${lojaFilter}|${imageFilter}|${sortConfig.key}:${sortConfig.direction}|${estoquePage}`
    }

    async function fetchEstoquePage() {
        const cacheKey = buildPageCacheKey()
        const cached = cacheGet(cacheKey)
        if (cached) {
            setEstoque(cached.items)
            setEstoqueTotal(cached.total)
            return
        }

        try {
            setIsFetchingEstoque(true)

            const cols = 'id_unico, id_produto, id_loja, loja, nome, tamanho, preco, preco_varejo, preco_atacado, estoque, grupo_id, nome_grupo, last_sync'
            let q = supabase
                .from('produtos_estoque')
                .select(cols, { count: 'exact' })

            if (estoqueSearchDebounced) {
                const safe = estoqueSearchDebounced.replace(/[,()%]/g, ' ')
                q = q.or(`nome.ilike.%${safe}%,id_produto.ilike.%${safe}%,tamanho.ilike.%${safe}%`)
            }
            if (lojaFilter !== 'all') q = q.eq('loja', lojaFilter)

            if (idsComImagem.length > 0) {
                if (imageFilter === 'present') {
                    q = q.in('id_produto', idsComImagem)
                } else if (imageFilter === 'missing') {
                    // .not('id_produto', 'in', '("a","b")') — formato Postgrest
                    const list = idsComImagem.map(id => `"${String(id).replace(/"/g, '\\"')}"`).join(',')
                    q = q.not('id_produto', 'in', `(${list})`)
                }
            }

            const sortKey = ESTOQUE_SORT_ALLOWED.has(sortConfig.key) ? sortConfig.key : 'nome'
            q = q.order(sortKey, { ascending: sortConfig.direction === 'asc', nullsFirst: false })

            const from = estoquePage * ESTOQUE_PAGE_SIZE
            const to = from + ESTOQUE_PAGE_SIZE - 1
            q = q.range(from, to)

            const { data, error, count } = await q
            if (error) throw error

            const items = (data || []).map(item => ({
                ...item,
                imagem_url: imagensMap[item.id_produto] || null
            }))

            setEstoque(items)
            setEstoqueTotal(count || 0)
            cacheSet(cacheKey, { items, total: count || 0 })
        } catch (error) {
            console.error('Error fetching estoque page:', error)
        } finally {
            setIsFetchingEstoque(false)
        }
    }

    async function refreshEstoque() {
        cacheClearAll()
        setEstoquePage(0)
        await loadEstoqueMetadados({ useCache: false })
        // fetchEstoquePage será chamado pelo effect quando idsComImagem atualizar
    }

    async function handleImageUpload(e, produtoId) {
        const file = e.target.files?.[0]
        if (!file) return

        try {
            setUploadingProductId(produtoId)
            const fileExt = file.name.split('.').pop()
            const fileName = `${produtoId}-${Math.random().toString(36).substring(2)}.${fileExt}`
            const filePath = `produtos/${fileName}`

            // Upload via Supabase Storage
            const { error: uploadError } = await supabase.storage
                .from('produtos')
                .upload(filePath, file)

            if (uploadError) throw uploadError

            // Pegar Public URL
            const { data: { publicUrl } } = supabase.storage
                .from('produtos')
                .getPublicUrl(filePath)

            // Verificar se imagem já existe na tabela separada
            const { data: existingRecords } = await supabase
                .from('produtos_imagens')
                .select('id')
                .eq('produto_id', produtoId)
                .limit(1)

            if (existingRecords && existingRecords.length > 0) {
                // Atualiza o existente
                const { error: updateError } = await supabase
                    .from('produtos_imagens')
                    .update({ imagem_url: publicUrl })
                    .eq('produto_id', produtoId)
                if (updateError) throw updateError
            } else {
                // Insere um novo
                const { error: insertError } = await supabase
                    .from('produtos_imagens')
                    .insert([{ produto_id: produtoId, imagem_url: publicUrl }])
                if (insertError) throw insertError
            }

            // Atualizar UI State + mapa de imagens + cache
            setEstoque(prev => prev.map(item =>
                item.id_produto === produtoId ? { ...item, imagem_url: publicUrl } : item
            ))
            setImagensMap(prev => {
                const next = { ...prev, [produtoId]: publicUrl }
                cacheSet('imagens', next)
                return next
            })
            setIdsComImagem(prev => prev.includes(produtoId) ? prev : [...prev, produtoId])
            // Invalida cache de páginas (filtros por imagem podem mudar)
            try {
                for (let i = sessionStorage.length - 1; i >= 0; i--) {
                    const k = sessionStorage.key(i)
                    if (k && k.startsWith(`${ESTOQUE_CACHE_PREFIX}:page:`)) sessionStorage.removeItem(k)
                }
            } catch {}

        } catch (error) {
            alert('Erro ao fazer upload da imagem: ' + error.message)
            console.error(error)
        } finally {
            setUploadingProductId(null)
        }
    }

    function groupLogsToConversations(logs) {
        const grouped = {}
        for (const log of logs) {
            const uid = log.user_id || 'desconhecido'
            if (!grouped[uid]) grouped[uid] = []
            grouped[uid].push(log)
        }
        return Object.entries(grouped)
            .map(([userId, messages]) => ({
                userId,
                messages: messages.sort((a, b) => new Date(a.created_at) - new Date(b.created_at)),
                lastMessage: messages[0],
                messageCount: messages.length
            }))
            .sort((a, b) => new Date(b.lastMessage.created_at) - new Date(a.lastMessage.created_at))
    }

    const conversations = groupLogsToConversations(logs)
    const activeConvo = conversations.find(c => c.userId === selectedConversation)

    function formatPhone(userId) {
        if (!userId || userId.length < 10) return userId
        const clean = userId.replace(/\D/g, '')
        if (clean.length === 13) return `+${clean.slice(0, 2)} ${clean.slice(2, 4)} ${clean.slice(4, 9)}-${clean.slice(9)}`
        if (clean.length === 12) return `+${clean.slice(0, 2)} ${clean.slice(2, 4)} ${clean.slice(4, 8)}-${clean.slice(8)}`
        return clean
    }

    function timeAgo(dateStr) {
        const diff = Date.now() - new Date(dateStr).getTime()
        const mins = Math.floor(diff / 60000)
        if (mins < 1) return 'agora'
        if (mins < 60) return `${mins}m`
        const hours = Math.floor(mins / 60)
        if (hours < 24) return `${hours}h`
        return `${Math.floor(hours / 24)}d`
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

    async function saveOperatorNumber() {
        setSavingOperator(true)
        try {
            const cleaned = (operatorNumber || '').replace(/\D/g, '')
            const { error } = await supabase
                .from('bot_settings')
                .update({ operator_number: cleaned || null, updated_at: new Date() })
                .eq('id', 1)
            if (error) throw error
            setConfig({ ...config, operator_number: cleaned })
            setOperatorNumber(cleaned)
            setOperatorSaved(true)
            setTimeout(() => setOperatorSaved(false), 2000)
        } catch (error) {
            alert('Erro ao salvar número do atendente: ' + error.message)
        } finally {
            setSavingOperator(false)
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

    function discardChanges() {
        if (confirm('Descartar alterações e recarregar o prompt atual?')) {
            fetchConfig()
            setSaveSuccess(false)
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
            id: Math.random().toString(36).substring(2, 11),
            title: 'NOVO BLOCO',
            content: '',
            collapsed: false
        }])
    }

    function handleSort(key) {
        let direction = 'asc'
        if (sortConfig.key === key && sortConfig.direction === 'asc') direction = 'desc'
        setSortConfig({ key, direction })
    }

    // Filtragem/ordenação são feitas no servidor. `estoque` já contém a página corrente.
    const estoqueTotalPages = Math.max(1, Math.ceil(estoqueTotal / ESTOQUE_PAGE_SIZE))

    if (loading) return (
        <div className="min-h-screen flex items-center justify-center bg-slate-50 dark:bg-[#0A0A0A] text-slate-500 dark:text-slate-400 text-sm font-sans">
            <div className="flex flex-col items-center gap-4">
                <div className="w-8 h-8 rounded-full border-2 border-purple-500/30 border-t-purple-500 animate-spin"></div>
                Carregando Dashboard...
            </div>
        </div>
    )

    // Calculate metrics
    const msgsToday = logs.filter(log => {
        const date = new Date(log.created_at)
        const today = new Date()
        return date.getDate() === today.getDate() && date.getMonth() === today.getMonth() && date.getFullYear() === today.getFullYear()
    }).length

    return (
        <div className="flex h-screen bg-slate-50 dark:bg-[#0A0A0A] text-slate-800 dark:text-slate-200 overflow-hidden font-sans transition-colors duration-300">

            {/* 1. SIDEBAR (Compact 88px) */}
            <aside className="w-[88px] bg-white dark:bg-[#0A0A0A] border-r border-slate-200 dark:border-[#1E1E1E] flex flex-col shrink-0 z-20">
                {/* Logo Area */}
                <div className="h-16 flex items-center justify-center border-b border-slate-200 dark:border-[#1E1E1E] shrink-0">
                    <div className="w-8 h-8 rounded-lg bg-purple-600 flex items-center justify-center shadow-md">
                        <Bot size={20} className="text-white relative top-[1px]" />
                    </div>
                </div>

                {/* Navigation Menu */}
                <nav className="flex-1 px-3 py-5 space-y-2 overflow-y-auto custom-scrollbar">
                    <SidebarItem id="dashboard" icon={LayoutDashboard} label="Ouvidoria" active={activeSidebarItem === 'dashboard'} onClick={() => setActiveSidebarItem('dashboard')} />
                    <SidebarItem id="inbox" icon={MessageSquare} label="Inbox" active={activeSidebarItem === 'inbox'} onClick={() => setActiveSidebarItem('inbox')} />

                    <div className="my-4 h-px bg-slate-200 dark:bg-[#1E1E1E] mx-2"></div>

                    <SidebarItem id="comandos" icon={Command} label="Comandos" active={activeSidebarItem === 'comandos'} onClick={() => setActiveSidebarItem('comandos')} />
                    <SidebarItem id="estoque" icon={Package} label="Estoque" active={activeSidebarItem === 'estoque'} onClick={() => setActiveSidebarItem('estoque')} />
                    {/* <SidebarItem id="integracoes" icon={Plug} label="Integrações" active={activeSidebarItem === 'integracoes'} onClick={() => setActiveSidebarItem('integracoes')} /> */}
                    {/* <SidebarItem id="sincronizacao" icon={RefreshCw} label="Sinc. Dados" active={activeSidebarItem === 'sincronizacao'} onClick={() => setActiveSidebarItem('sincronizacao')} /> */}
                </nav>
            </aside>

            {/* MAIN AREA */}
            <div className="flex-1 flex flex-col min-w-0 bg-slate-50 dark:bg-[#0A0A0A] transition-colors duration-300">
                {/* 2. TOP HEADER */}
                <header className="h-16 flex items-center justify-between px-6 border-b border-slate-200 dark:border-[#1E1E1E] bg-slate-50 dark:bg-[#0A0A0A] shrink-0 z-10 transition-colors">
                    <div className="flex items-center gap-4">
                        <h1 className="text-[15px] font-medium text-slate-800 dark:text-slate-200 capitalize tracking-wide transition-colors">
                            {activeSidebarItem === 'integracoes' ? 'Integrações' :
                                activeSidebarItem === 'sincronizacao' ? 'Sincronização' :
                                    activeSidebarItem === 'estoque' ? 'Controle de Estoque' : activeSidebarItem}
                        </h1>
                    </div>
                    <div className="flex items-center gap-4">
                        <button onClick={toggleTheme} className="p-2 -mr-1 rounded-full hover:bg-slate-200 dark:hover:bg-[#1A1A1A] text-slate-500 dark:text-[#71717A] transition-colors">
                            {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
                        </button>
                        <div className="w-px h-5 bg-slate-200 dark:bg-[#1E1E1E]"></div>
                        <span className="text-xs text-slate-500 dark:text-[#A1A1AA] font-medium hidden sm:block transition-colors">diogoraniceto@gmail.com</span>
                        <div className="w-8 h-8 rounded-full bg-purple-100 dark:bg-[#1E1430] flex items-center justify-center text-[11px] font-bold text-purple-600 dark:text-purple-400 border border-purple-200 dark:border-purple-500/20 cursor-pointer transition-colors">
                            DI
                        </div>
                    </div>
                </header>

                {/* 3. SCROLLABLE CONTENT AREA */}
                <main className="flex-1 overflow-auto custom-scrollbar relative">

                    {/* DASHBOARD PAGE */}
                    {activeSidebarItem === 'dashboard' && (
                        <div className="p-8 max-w-5xl mx-auto space-y-6 animate-fade-in pb-20">
                            {/* Switch Card */}
                            <div className="bg-white dark:bg-[#0F0F0F] border border-slate-200 dark:border-[#1E1E1E] rounded-xl p-6 flex flex-row items-center justify-between shadow-sm transition-colors">
                                <div className="flex items-center gap-4">
                                    <div className={`w-2.5 h-2.5 rounded-full shadow-sm ${config?.is_active ? 'bg-emerald-500 shadow-emerald-500/40' : 'bg-red-500 shadow-red-500/40'}`}></div>
                                    <div className="flex flex-col">
                                        <h3 className="text-slate-800 dark:text-slate-200 font-semibold text-[15px] transition-colors">Bot de Vendas</h3>
                                        <p className="text-slate-500 dark:text-[#71717A] text-[13px] mt-0.5 font-medium transition-colors">
                                            {config?.is_active ? 'Online — respondendo clientes' : 'Offline — atividades pausadas'}
                                        </p>
                                    </div>
                                </div>
                                {/* Simple Toggle Switch */}
                                <button
                                    onClick={toggleBot}
                                    className={`w-12 h-6 rounded-full transition-colors duration-300 relative cursor-pointer border ${config?.is_active ? 'bg-purple-600 border-purple-500' : 'bg-slate-300 dark:bg-[#222] border-slate-400 dark:border-[#333]'}`}
                                >
                                    <div className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all duration-300 shadow-sm ${config?.is_active ? 'left-[22px]' : 'left-1'}`}></div>
                                </button>
                            </div>

                            {/* Atendente humano (handoff) */}
                            <div className="bg-white dark:bg-[#0F0F0F] border border-slate-200 dark:border-[#1E1E1E] rounded-xl p-6 shadow-sm transition-colors">
                                <div className="flex flex-col gap-1 mb-4">
                                    <h3 className="text-slate-800 dark:text-slate-200 font-semibold text-[15px]">Atendente humano</h3>
                                    <p className="text-slate-500 dark:text-[#71717A] text-[13px] font-medium">
                                        Número que receberá o resumo quando o bot transferir a conversa.
                                    </p>
                                </div>
                                <div className="flex items-center gap-3">
                                    <input
                                        type="tel"
                                        placeholder="5511999998888"
                                        value={operatorNumber}
                                        onChange={(e) => setOperatorNumber(e.target.value)}
                                        className="flex-1 bg-slate-50 dark:bg-[#0A0A0A] border border-slate-200 dark:border-[#1E1E1E] rounded-lg px-3 py-2 text-sm text-slate-800 dark:text-slate-200 placeholder:text-slate-400 dark:placeholder:text-[#71717A] focus:outline-none focus:border-purple-500"
                                    />
                                    <button
                                        onClick={saveOperatorNumber}
                                        disabled={savingOperator}
                                        className="px-4 py-2 rounded-lg bg-purple-600 hover:bg-purple-700 disabled:opacity-50 text-white text-sm font-semibold transition-colors cursor-pointer"
                                    >
                                        {savingOperator ? 'Salvando...' : operatorSaved ? 'Salvo ✓' : 'Salvar'}
                                    </button>
                                </div>
                                <p className="text-slate-400 dark:text-[#71717A] text-[11px] mt-2">
                                    Formato internacional sem "+" (ex: 5511999998888).
                                </p>
                            </div>

                            {/* Metrics Grids */}
                            {/* Card 1 */}
                            <div className="bg-white dark:bg-[#0F0F0F] border border-slate-200 dark:border-[#1E1E1E] rounded-xl p-5 relative shadow-sm transition-colors">
                                <MessageSquare size={18} className="absolute top-5 right-5 text-slate-300 dark:text-[#333] transition-colors" />
                                <h4 className="text-slate-500 dark:text-[#A1A1AA] text-[13px] font-medium mb-3 transition-colors">Conversas</h4>
                                <div className="text-3xl font-bold text-slate-800 dark:text-slate-100 mb-1 transition-colors">{conversations.length}</div>
                                <p className="text-slate-400 dark:text-[#71717A] text-[11px] font-medium transition-colors">Total de contatos</p>
                            </div>
                            {/* Card 2 */}
                            <div className="bg-white dark:bg-[#0F0F0F] border border-slate-200 dark:border-[#1E1E1E] rounded-xl p-5 relative shadow-sm transition-colors">
                                <MessageCircle size={18} className="absolute top-5 right-5 text-slate-300 dark:text-[#333] transition-colors" />
                                <h4 className="text-slate-500 dark:text-[#A1A1AA] text-[13px] font-medium mb-3 transition-colors">Mensagens Hoje</h4>
                                <div className="text-3xl font-bold text-slate-800 dark:text-slate-100 mb-1 transition-colors">{msgsToday}</div>
                                <p className="text-slate-400 dark:text-[#71717A] text-[11px] font-medium transition-colors">Enviadas e recebidas</p>
                            </div>
                            {/* Card 3 */}
                            <div className="bg-white dark:bg-[#0F0F0F] border border-slate-200 dark:border-[#1E1E1E] rounded-xl p-5 relative shadow-sm transition-colors">
                                <Activity size={18} className="absolute top-5 right-5 text-slate-300 dark:text-[#333] transition-colors" />
                                <h4 className="text-slate-500 dark:text-[#A1A1AA] text-[13px] font-medium mb-3 transition-colors">Status</h4>
                                <div className="text-3xl font-bold text-slate-800 dark:text-slate-100 mb-1 flex items-center gap-3 transition-colors">
                                    <div className={`w-3 h-3 rounded-full mt-1 ${config?.is_active ? 'bg-emerald-500' : 'bg-red-500'}`}></div>
                                    {config?.is_active ? 'Online' : 'Offline'}
                                </div>
                                <p className="text-slate-400 dark:text-[#71717A] text-[11px] font-medium transition-colors">Estado atual do bot</p>
                            </div>
                        </div>
                    )}

                    {/* INBOX PAGE */}
                    {activeSidebarItem === 'inbox' && (
                        <div className="flex h-full max-h-full">
                            {/* Coluna 1 (Lista) */}
                            <div className="w-[340px] border-r border-slate-200 dark:border-[#1E1E1E] bg-slate-50 dark:bg-[#0A0A0A] flex flex-col shrink-0 transition-colors">
                                <div className="p-4 border-b border-slate-200 dark:border-[#1E1E1E] flex flex-col gap-3 transition-colors">
                                    <h2 className="text-[11px] font-bold text-slate-500 dark:text-[#71717A] tracking-widest mt-1 transition-colors">
                                        CONVERSAS
                                    </h2>
                                </div>
                                <div className="flex-1 overflow-y-auto custom-scrollbar p-2 space-y-0.5">
                                    {conversations.length === 0 ? (
                                        <div className="text-slate-500 dark:text-[#71717A] text-center py-10 text-xs transition-colors">Nenhuma conversa.</div>
                                    ) : (
                                        conversations.map((convo) => (
                                            <button
                                                key={convo.userId}
                                                onClick={() => setSelectedConversation(convo.userId)}
                                                className={`w-full text-left p-3 rounded-lg transition-all duration-200 cursor-pointer flex items-center gap-3 ${selectedConversation === convo.userId ? 'bg-slate-200 dark:bg-[#1E1E1E]' : 'hover:bg-slate-100 dark:hover:bg-[#111111]'}`}
                                            >
                                                <div className={`w-9 h-9 rounded-full flex shrink-0 items-center justify-center text-sm font-semibold border ${selectedConversation === convo.userId ? 'bg-purple-100 dark:bg-[#1E1430] text-purple-600 dark:text-purple-400 border-purple-200 dark:border-purple-500/20' : 'bg-slate-100 dark:bg-[#1A1A1A] text-slate-500 dark:text-[#71717A] border-slate-200 dark:border-[#222]'}`}>
                                                    <UserCircle2 size={18} />
                                                </div>
                                                <div className="flex-1 min-w-0">
                                                    <div className="flex justify-between items-center mb-0.5">
                                                        <span className={`text-[13px] font-semibold truncate transition-colors ${selectedConversation === convo.userId ? 'text-slate-900 dark:text-slate-200' : 'text-slate-700 dark:text-slate-300'}`}>
                                                            {formatPhone(convo.userId)}
                                                        </span>
                                                        <span className="text-[10px] text-slate-500 dark:text-[#71717A] whitespace-nowrap ml-2 transition-colors">
                                                            {timeAgo(convo.lastMessage.created_at)}
                                                        </span>
                                                    </div>
                                                    <p className={`text-[12px] truncate transition-colors ${selectedConversation === convo.userId ? 'text-slate-600 dark:text-[#A1A1AA]' : 'text-slate-500 dark:text-[#71717A]'}`}>
                                                        {convo.lastMessage.content}
                                                    </p>
                                                </div>
                                            </button>
                                        ))
                                    )}
                                </div>
                            </div>

                            {/* Coluna 2 (Chat) */}
                            <div className="flex-1 flex flex-col bg-white dark:bg-[#0F0F0F] relative transition-colors">
                                {selectedConversation && activeConvo ? (
                                    <>
                                        <div className="px-6 py-4 border-b border-slate-200 dark:border-[#1E1E1E] bg-slate-50 dark:bg-[#0A0A0A] sticky top-0 z-10 flex justify-between items-center transition-colors">
                                            <div className="flex items-center gap-3">
                                                <div className="w-10 h-10 rounded-full bg-purple-100 dark:bg-[#1E1430] text-purple-600 dark:text-purple-400 flex items-center justify-center border border-purple-200 dark:border-purple-500/20 transition-colors">
                                                    <UserCircle2 size={24} />
                                                </div>
                                                <div>
                                                    <h3 className="font-semibold text-slate-900 dark:text-slate-100 text-[15px] transition-colors">{formatPhone(activeConvo.userId)}</h3>
                                                    <p className="text-[11px] font-medium text-slate-500 dark:text-[#71717A] flex items-center gap-1.5 mt-0.5 transition-colors">
                                                        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
                                                        Sessão Ativa
                                                    </p>
                                                </div>
                                            </div>
                                            <button className="p-2 rounded-lg text-slate-500 dark:text-[#71717A] hover:text-slate-800 dark:hover:text-slate-300 hover:bg-slate-200 dark:hover:bg-[#1A1A1A] transition-colors cursor-pointer border border-transparent">
                                                <Settings size={18} />
                                            </button>
                                        </div>

                                        <div className="flex-1 overflow-y-auto custom-scrollbar p-6 space-y-6">
                                            <div className="text-center my-4">
                                                <span className="text-[10px] font-medium px-3 py-1 bg-slate-100 dark:bg-[#1A1A1A] text-slate-500 dark:text-[#71717A] rounded-md border border-slate-200 dark:border-[#222] transition-colors">
                                                    Histórico
                                                </span>
                                            </div>
                                            {activeConvo.messages.map((msg) => (
                                                <div
                                                    key={msg.id}
                                                    className={`flex flex-col max-w-[75%] ${msg.role === 'user' ? 'ml-auto items-end' : 'mr-auto items-start'}`}
                                                >
                                                    <div className="flex items-baseline gap-2 mb-1 px-1 mt-2">
                                                        <span className="text-[10px] font-medium text-slate-500 dark:text-[#A1A1AA] transition-colors">{msg.role === 'user' ? 'Cliente' : 'Bot Assistente'}</span>
                                                        <span className="text-[9px] text-slate-400 dark:text-[#71717A] transition-colors">{new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                                                    </div>
                                                    <div
                                                        className={`p-3.5 rounded-xl text-[13px] leading-relaxed relative border transition-colors ${msg.role === 'user'
                                                            ? 'bg-slate-100 dark:bg-[#1E1E1E] text-slate-800 dark:text-slate-200 border-slate-200 dark:border-[#333] rounded-tr-sm shadow-sm'
                                                            : 'bg-purple-600 dark:bg-purple-600 border-purple-500 dark:border-purple-500 text-white rounded-tl-sm shadow-sm opacity-95'
                                                            }`}
                                                    >
                                                        <p className="whitespace-pre-wrap">{msg.content}</p>
                                                    </div>
                                                </div>
                                            ))}
                                            <div ref={messagesEndRef} />
                                        </div>
                                    </>
                                ) : (
                                    <div className="flex-1 flex flex-col items-center justify-center text-slate-500 dark:text-[#71717A] p-8 text-center transition-colors">
                                        <MessageSquare size={36} className="text-slate-300 dark:text-[#333] mb-4 transition-colors" />
                                        <p className="text-sm font-medium">Selecione uma conversa</p>
                                    </div>
                                )}
                            </div>
                        </div>
                    )}

                    {/* COMANDOS PAGE (Prompt Builder) */}
                    {activeSidebarItem === 'comandos' && (
                        <div className="px-8 py-8 max-w-4xl mx-auto w-full pb-24 animate-fade-in transition-colors">
                            {/* Header de Ações (Apenas p/ Comandos) */}
                            <div className="flex justify-between items-center mb-6">
                                <div>
                                    <h2 className="text-xl font-bold text-slate-900 dark:text-slate-100 transition-colors">Construtor de Comandos</h2>
                                    <p className="text-slate-500 dark:text-[#A1A1AA] text-xs mt-1 transition-colors">Configure as diretrizes de comportamento do bot.</p>
                                </div>
                                <div className="flex items-center gap-3">
                                    <button
                                        onClick={discardChanges}
                                        className="px-4 py-2 rounded-lg text-xs font-semibold text-slate-500 dark:text-[#A1A1AA] hover:text-slate-800 dark:hover:text-slate-200 hover:bg-slate-200 dark:hover:bg-[#1E1E1E] transition-colors cursor-pointer border border-slate-300 dark:border-[#222]"
                                    >
                                        Descartar
                                    </button>
                                    <button
                                        onClick={savePrompt}
                                        disabled={saving}
                                        className={`flex items-center gap-2 px-5 py-2 rounded-lg transition-colors duration-200 text-xs font-semibold cursor-pointer disabled:opacity-50 border ${saveSuccess
                                            ? 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-200 dark:border-emerald-500/20'
                                            : 'bg-purple-600 hover:bg-purple-500 text-white border-purple-500 shadow-sm'
                                            }`}
                                    >
                                        <Save size={14} />
                                        {saving ? 'Salvando...' : saveSuccess ? 'Salvo ✓' : 'Salvar Alterações'}
                                    </button>
                                </div>
                            </div>

                            <div className="bg-white dark:bg-[#0F0F0F] rounded-2xl border border-slate-200 dark:border-[#1E1E1E] p-6 shadow-sm transition-colors">
                                {/* Blocks */}
                                <div
                                    className="space-y-4"
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
                                    className="w-full mt-6 py-4 border border-dashed border-slate-300 dark:border-[#333] hover:border-purple-500/50 dark:hover:border-purple-500/50 bg-slate-50 dark:bg-transparent hover:bg-slate-100 dark:hover:bg-[#111111] rounded-xl text-slate-500 dark:text-[#71717A] hover:text-purple-600 dark:hover:text-purple-400 transition-all duration-200 flex items-center justify-center gap-2 text-xs font-bold uppercase tracking-wider group cursor-pointer"
                                >
                                    <Plus size={14} className="group-hover:text-purple-600 dark:group-hover:text-purple-400 transition-colors" />
                                    Adicionar Nova Sessão
                                </button>
                            </div>
                        </div>
                    )}

                    {/* ESTOQUE PAGE */}
                    {activeSidebarItem === 'estoque' && (
                        <div className="px-8 pt-8 pb-6 w-full max-w-[1700px] 2xl:max-w-none mx-auto h-full min-h-0 flex flex-col animate-fade-in relative transition-colors">
                            <div className="flex justify-between items-end mb-6 shrink-0">
                                <div>
                                    <h2 className="text-xl font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2 transition-colors">
                                        Controle de Estoque
                                        <span className="text-[10px] bg-purple-50 dark:bg-[#1E1430] text-purple-600 dark:text-purple-400 border border-purple-200 dark:border-purple-500/20 px-2 py-0.5 rounded-full uppercase tracking-wider font-semibold transition-colors">
                                            {estoqueTotal} ITENS
                                        </span>
                                    </h2>
                                    <p className="text-slate-500 dark:text-[#A1A1AA] text-xs mt-1 transition-colors">
                                        Última sincronização: <span className="text-slate-700 dark:text-slate-200 font-medium">
                                            {lastSyncDate ? lastSyncDate.toLocaleString() : 'Desconhecida'}
                                        </span>
                                    </p>
                                </div>
                                <div className="flex items-center gap-3">
                                    {/* View Toggles */}
                                    <div className="flex items-center bg-slate-100 dark:bg-[#1A1A1A] rounded-lg p-1 border border-slate-200 dark:border-[#222]">
                                        <button
                                            onClick={() => setViewMode('gallery')}
                                            className={`p-1.5 flex items-center justify-center rounded-md transition-all ${viewMode === 'gallery' ? 'bg-white dark:bg-[#333] shadow-sm text-slate-900 dark:text-slate-100' : 'text-slate-500 dark:text-[#71717A] hover:text-slate-700 dark:hover:text-slate-300'}`}
                                            title="Vista em Galeria (Cards)"
                                        >
                                            <LayoutGrid size={16} />
                                        </button>
                                        <button
                                            onClick={() => setViewMode('table')}
                                            className={`p-1.5 flex items-center justify-center rounded-md transition-all ${viewMode === 'table' ? 'bg-white dark:bg-[#333] shadow-sm text-slate-900 dark:text-slate-100' : 'text-slate-500 dark:text-[#71717A] hover:text-slate-700 dark:hover:text-slate-300'}`}
                                            title="Vista em Tabela (Planilha)"
                                        >
                                            <List size={16} />
                                        </button>
                                    </div>

                                    {/* Filtro de imagem */}
                                    <button
                                        onClick={() => {
                                            if (imageFilter === 'all') setImageFilter('missing')
                                            else if (imageFilter === 'missing') setImageFilter('present')
                                            else setImageFilter('all')
                                        }}
                                        className={`px-3 py-2 border rounded-lg flex items-center gap-2 text-xs font-semibold transition-colors ${imageFilter === 'missing'
                                            ? 'bg-amber-50 dark:bg-amber-500/10 border-amber-200 dark:border-amber-500/20 text-amber-600 dark:text-amber-400'
                                            : imageFilter === 'present'
                                                ? 'bg-emerald-50 dark:bg-emerald-500/10 border-emerald-200 dark:border-emerald-500/20 text-emerald-600 dark:text-emerald-400'
                                                : 'bg-white dark:bg-[#0F0F0F] border-slate-300 dark:border-[#1E1E1E] text-slate-500 dark:text-[#71717A] hover:bg-slate-50 dark:hover:bg-[#1A1A1A]'
                                            }`}
                                        title={
                                            imageFilter === 'missing'
                                                ? 'Mostrando produtos sem imagem'
                                                : imageFilter === 'present'
                                                    ? 'Mostrando produtos com imagem'
                                                    : 'Mostrando todos os produtos'
                                        }
                                    >
                                        <Camera size={14} />
                                        Filtro
                                    </button>

                                    {/* Filtro de loja */}
                                    <select
                                        value={lojaFilter}
                                        onChange={(e) => setLojaFilter(e.target.value)}
                                        className="px-3 py-2 border rounded-lg text-xs font-semibold transition-colors bg-white dark:bg-[#0F0F0F] border-slate-300 dark:border-[#1E1E1E] text-slate-600 dark:text-[#A1A1AA] focus:outline-none focus:border-purple-500/50 shadow-sm cursor-pointer"
                                    >
                                        <option value="all">Todas as Lojas</option>
                                        {lojasList.map(loja => (
                                            <option key={loja} value={loja}>{loja}</option>
                                        ))}
                                    </select>

                                    <div className="relative">
                                        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 dark:text-[#71717A] transition-colors" />
                                        <input
                                            type="text"
                                            placeholder="Buscar produto, ID ou tamanho..."
                                            value={estoqueSearch}
                                            onChange={(e) => setEstoqueSearch(e.target.value)}
                                            className="w-64 bg-white dark:bg-[#0F0F0F] border border-slate-300 dark:border-[#1E1E1E] focus:border-purple-500/50 dark:focus:border-purple-500/50 rounded-lg pl-9 pr-4 py-2 text-xs text-slate-800 dark:text-slate-200 placeholder:text-slate-400 dark:placeholder:text-[#71717A] focus:outline-none transition-colors shadow-sm"
                                        />
                                    </div>
                                    <button
                                        onClick={refreshEstoque}
                                        disabled={isFetchingEstoque}
                                        className="p-2 border border-slate-300 dark:border-[#1E1E1E] bg-white dark:bg-[#0F0F0F] rounded-lg text-slate-500 dark:text-[#71717A] hover:text-slate-800 dark:hover:text-slate-300 hover:bg-slate-50 dark:hover:bg-[#1A1A1A] transition-colors cursor-pointer disabled:opacity-50 shadow-sm"
                                        title="Atualizar (limpa cache)"
                                    >
                                        <RefreshCw size={16} className={isFetchingEstoque ? 'animate-spin' : ''} />
                                    </button>
                                </div>
                            </div>


                            <div className="flex-1 min-h-0 bg-white dark:bg-[#0F0F0F] rounded-2xl border border-slate-200 dark:border-[#1E1E1E] shadow-sm overflow-hidden flex flex-col transition-colors">
                                {isFetchingEstoque && estoque.length === 0 ? (
                                    <div className="flex-1 flex items-center justify-center text-slate-500 dark:text-[#71717A] text-sm transition-colors">
                                        <Activity size={18} className="animate-spin mr-2 text-slate-400 dark:text-[#333]" /> Carregando estoque...
                                    </div>
                                ) : viewMode === 'table' ? (
                                    <div className="overflow-auto custom-scrollbar flex-1 min-h-0 relative">
                                        <table className="w-full text-left text-[13px] border-collapse">
                                            <thead className="sticky top-0 z-10 bg-slate-50 dark:bg-[#0A0A0A] shadow-[0_1px_0_0_#e2e8f0] dark:shadow-[0_1px_0_0_#1E1E1E] transition-colors">
                                                <tr className="text-slate-500 dark:text-[#A1A1AA] text-[11px] uppercase tracking-wider font-semibold transition-colors">
                                                    {[
                                                        { key: 'loja', label: 'loja' },
                                                        { key: 'id_produto', label: 'id_produto' },
                                                        { key: 'id_variacao', label: 'id_variacao' },
                                                        { key: 'nome', label: 'nome' },
                                                        { key: 'tamanho', label: 'tamanho' },
                                                        { key: 'preco', label: 'preco' },
                                                        { key: 'estoque', label: 'estoque' },
                                                        { key: 'last_sync', label: 'last_sync' },
                                                        { key: 'preco_varejo', label: 'preco_varejo' },
                                                        { key: 'preco_atacado', label: 'preco_atacado' }
                                                    ].map(col => (
                                                        <th
                                                            key={col.key}
                                                            className="px-5 py-3 cursor-pointer hover:bg-slate-100 dark:hover:bg-[#1A1A1A] transition-colors whitespace-nowrap"
                                                            onClick={() => handleSort(col.key)}
                                                        >
                                                            <div className="flex items-center gap-1.5">
                                                                {col.label}
                                                                {sortConfig.key === col.key ? (
                                                                    sortConfig.direction === 'asc' ? <ArrowUp size={12} className="text-purple-600 dark:text-purple-400" /> : <ArrowDown size={12} className="text-purple-600 dark:text-purple-400" />
                                                                ) : (
                                                                    <ArrowUpDown size={12} className="text-slate-300 dark:text-[#333] opacity-0 group-hover:opacity-100" />
                                                                )}
                                                            </div>
                                                        </th>
                                                    ))}
                                                </tr>
                                            </thead>
                                            <tbody className="divide-y divide-slate-100 dark:divide-[#1E1E1E]/50 transition-colors">
                                                {estoque.map((item) => (
                                                    <tr key={item.id_unico} className="hover:bg-slate-50 dark:hover:bg-[#141414] transition-colors group">
                                                        <td className="px-5 py-3.5 text-slate-600 dark:text-[#A1A1AA] text-[11px] font-medium whitespace-nowrap">{item.loja || '-'}</td>
                                                        <td className="px-5 py-3.5 text-slate-500 dark:text-[#71717A] font-mono text-[10px]">{item.id_produto || '-'}</td>
                                                        <td className="px-5 py-3.5 text-slate-500 dark:text-[#71717A] font-mono text-[10px]">{item.id_variacao || '-'}</td>
                                                        <td className="px-5 py-3.5 font-medium text-slate-800 dark:text-slate-200 whitespace-nowrap">{item.nome || '-'}</td>
                                                        <td className="px-5 py-3.5 text-slate-600 dark:text-[#A1A1AA]">{item.tamanho || '-'}</td>
                                                        <td className="px-5 py-3.5 text-slate-500 dark:text-[#71717A] text-xs whitespace-nowrap">
                                                            {item.preco != null ? `R$ ${Number(item.preco).toFixed(2)}` : '-'}
                                                        </td>
                                                        <td className="px-5 py-3.5 text-slate-800 dark:text-slate-200">
                                                            <span className={`px-2 py-0.5 rounded-full text-[11px] font-bold ${item.estoque > 10 ? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400' :
                                                                item.estoque > 0 ? 'bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400' :
                                                                    'bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-400'
                                                                }`}>
                                                                {item.estoque || 0}
                                                            </span>
                                                        </td>
                                                        <td className="px-5 py-3.5 text-slate-500 dark:text-[#71717A] text-[10px] whitespace-nowrap">
                                                            {item.last_sync ? new Date(item.last_sync).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) : '-'}
                                                        </td>
                                                        <td className="px-5 py-3.5 text-slate-500 dark:text-[#71717A] text-xs whitespace-nowrap">
                                                            {item.preco_varejo != null ? `R$ ${Number(item.preco_varejo).toFixed(2)}` : '-'}
                                                        </td>
                                                        <td className="px-5 py-3.5 text-slate-500 dark:text-[#71717A] text-xs whitespace-nowrap">
                                                            {item.preco_atacado != null ? `R$ ${Number(item.preco_atacado).toFixed(2)}` : '-'}
                                                        </td>
                                                    </tr>
                                                ))}
                                                {estoque.length === 0 && (
                                                    <tr>
                                                        <td colSpan="10" className="px-5 py-8 text-center text-slate-500 dark:text-[#71717A] text-xs">
                                                            Nenhum produto encontrado.
                                                        </td>
                                                    </tr>
                                                )}
                                            </tbody>
                                        </table>
                                    </div>
                                ) : (
                                    <div className="overflow-auto custom-scrollbar flex-1 min-h-0 p-6 relative">
                                        {estoque.length === 0 ? (
                                            <div className="flex flex-col items-center justify-center p-12 text-slate-500 dark:text-[#71717A] border-2 border-dashed border-slate-200 dark:border-[#333] rounded-2xl h-full">
                                                <ImagePlus size={32} className="mb-4 text-slate-300 dark:text-[#555]" />
                                                <p className="text-sm font-medium">Nenhum produto encontrado.</p>
                                            </div>
                                        ) : (
                                            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
                                                {estoque.map(item => (
                                                    <div key={item.id_unico} className={`bg-white dark:bg-[#0F0F0F] border rounded-xl overflow-hidden shadow-sm hover:shadow-md transition-all flex flex-col group ${item.estoque > 0 && !item.imagem_url ? 'border-amber-300 dark:border-amber-500/50 shadow-[0_0_10px_rgba(251,191,36,0.2)]' : 'border-slate-200 dark:border-[#222]'}`}>
                                                        {/* Image Space */}
                                                        <div className="aspect-square w-full bg-slate-50 dark:bg-[#111] relative overflow-hidden group/image">
                                                            {item.imagem_url ? (
                                                                <>
                                                                    <img src={item.imagem_url} alt={item.nome} className="w-full h-full object-cover" loading="lazy" />
                                                                    <label className="absolute inset-0 bg-black/40 opacity-0 group-hover/image:opacity-100 flex items-center justify-center cursor-pointer transition-opacity backdrop-blur-sm">
                                                                        {uploadingProductId === item.id_produto ? <Activity size={24} className="text-white animate-spin" /> : <UploadCloud size={24} className="text-white" />}
                                                                        <input type="file" className="hidden" accept="image/*" onChange={(e) => handleImageUpload(e, item.id_produto)} disabled={uploadingProductId === item.id_produto} />
                                                                    </label>
                                                                </>
                                                            ) : (
                                                                <label className="absolute inset-0 flex flex-col items-center justify-center cursor-pointer hover:bg-slate-100 dark:hover:bg-[#1A1A1A] transition-colors p-4 text-center">
                                                                    {uploadingProductId === item.id_produto ? (
                                                                        <Activity size={24} className="text-purple-500 animate-spin mb-2" />
                                                                    ) : (
                                                                        <ImagePlus size={32} className={`mb-2 ${item.estoque > 0 ? 'text-amber-500' : 'text-slate-300 dark:text-[#444]'}`} />
                                                                    )}
                                                                    <span className={`text-[10px] font-semibold uppercase tracking-wide px-2 py-1 rounded bg-white dark:bg-[#222] shadow-sm ${item.estoque > 0 ? 'text-amber-600 dark:text-amber-400 border border-amber-200 dark:border-amber-500/20' : 'text-slate-500 dark:text-[#A1A1AA] border border-slate-200 dark:border-[#333]'}`}>
                                                                        {uploadingProductId === item.id_produto ? 'Enviando...' : 'Adicionar Foto'}
                                                                    </span>
                                                                    <input type="file" className="hidden" accept="image/*" onChange={(e) => handleImageUpload(e, item.id_produto)} disabled={uploadingProductId === item.id_produto} />
                                                                </label>
                                                            )}

                                                            {/* Estoque Badge Floating */}
                                                            <div className="absolute top-2 right-2 z-10">
                                                                <span className={`px-2 py-0.5 rounded text-[10px] font-bold shadow-sm backdrop-blur-md ${item.estoque > 10 ? 'bg-emerald-500/90 text-white' : item.estoque > 0 ? 'bg-amber-500/90 text-white' : 'bg-red-500/90 text-white'}`}>
                                                                    {item.estoque} un.
                                                                </span>
                                                            </div>
                                                        </div>

                                                        {/* Details */}
                                                        <div className="p-3 flex flex-col flex-1">
                                                            <div className="flex items-center justify-between mb-1">
                                                                <p className="font-mono text-[9px] text-slate-400 dark:text-[#71717A]">{item.id_produto}</p>
                                                                {item.loja && (
                                                                    <span className="text-[8px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-500/10 text-blue-600 dark:text-blue-400 border border-blue-200 dark:border-blue-500/20">
                                                                        {item.loja}
                                                                    </span>
                                                                )}
                                                            </div>
                                                            <h3 className="font-semibold text-[11px] text-slate-800 dark:text-slate-200 line-clamp-2 leading-snug flex-1 flex-grow mb-2" title={item.nome}>{item.nome || 'Produto Sem Nome'}</h3>

                                                            <div className="flex items-center justify-between mt-auto pt-2 border-t border-slate-100 dark:border-[#1E1E1E]">
                                                                <div className="text-xs font-bold text-emerald-600 dark:text-emerald-400">
                                                                    {item.preco != null ? `R$ ${Number(item.preco).toFixed(2)}` : '-'}
                                                                </div>
                                                                <div className="text-[10px] text-slate-400 dark:text-[#555] font-medium px-1.5 py-0.5 bg-slate-100 dark:bg-[#111] rounded">
                                                                    {item.tamanho || 'UNI'}
                                                                </div>
                                                            </div>
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>

                            {/* PAGINAÇÃO */}
                            {estoqueTotal > 0 && (
                                <div className="flex items-center justify-between mt-4 shrink-0 text-xs text-slate-500 dark:text-[#A1A1AA]">
                                    <div>
                                        Página <span className="font-semibold text-slate-700 dark:text-slate-200">{estoquePage + 1}</span> de <span className="font-semibold text-slate-700 dark:text-slate-200">{estoqueTotalPages}</span>
                                        {' '}· {estoque.length} itens nesta página
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <button
                                            onClick={() => setEstoquePage(0)}
                                            disabled={estoquePage === 0 || isFetchingEstoque}
                                            className="px-3 py-1.5 border border-slate-300 dark:border-[#1E1E1E] bg-white dark:bg-[#0F0F0F] rounded-lg hover:bg-slate-50 dark:hover:bg-[#1A1A1A] transition-colors disabled:opacity-40 disabled:cursor-not-allowed shadow-sm"
                                        >« Primeira</button>
                                        <button
                                            onClick={() => setEstoquePage(p => Math.max(0, p - 1))}
                                            disabled={estoquePage === 0 || isFetchingEstoque}
                                            className="px-3 py-1.5 border border-slate-300 dark:border-[#1E1E1E] bg-white dark:bg-[#0F0F0F] rounded-lg hover:bg-slate-50 dark:hover:bg-[#1A1A1A] transition-colors disabled:opacity-40 disabled:cursor-not-allowed shadow-sm"
                                        >‹ Anterior</button>
                                        <button
                                            onClick={() => setEstoquePage(p => Math.min(estoqueTotalPages - 1, p + 1))}
                                            disabled={estoquePage >= estoqueTotalPages - 1 || isFetchingEstoque}
                                            className="px-3 py-1.5 border border-slate-300 dark:border-[#1E1E1E] bg-white dark:bg-[#0F0F0F] rounded-lg hover:bg-slate-50 dark:hover:bg-[#1A1A1A] transition-colors disabled:opacity-40 disabled:cursor-not-allowed shadow-sm"
                                        >Próxima ›</button>
                                        <button
                                            onClick={() => setEstoquePage(estoqueTotalPages - 1)}
                                            disabled={estoquePage >= estoqueTotalPages - 1 || isFetchingEstoque}
                                            className="px-3 py-1.5 border border-slate-300 dark:border-[#1E1E1E] bg-white dark:bg-[#0F0F0F] rounded-lg hover:bg-slate-50 dark:hover:bg-[#1A1A1A] transition-colors disabled:opacity-40 disabled:cursor-not-allowed shadow-sm"
                                        >Última »</button>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}

                    {/* PLACEHOLDER PAGES */}
                    {(activeSidebarItem === 'integracoes' || activeSidebarItem === 'sincronizacao') && (
                        <div className="flex flex-col items-center justify-center h-full text-slate-500 dark:text-[#71717A] p-8 text-center transition-colors">
                            <Plug size={36} className="text-slate-300 dark:text-[#333] mb-4 transition-colors" />
                            <h3 className="text-lg font-medium text-slate-600 dark:text-[#A1A1AA] mb-2 transition-colors">Em Breve</h3>
                            <p className="text-sm">Esta seção ainda está sendo construída.</p>
                        </div>
                    )}

                </main>
            </div >
        </div >
    )
}
