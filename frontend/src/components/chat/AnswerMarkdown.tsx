import type { Parent, PhrasingContent, Root, RootContent } from 'mdast'
import { createContext, useContext, type ComponentPropsWithoutRef } from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

const MARKER = /\[S(\d{1,3})\]/g
const CITE_PREFIX = '#cite-'

/** Turn [S1] markers in text nodes into links to `#cite-1`, rendered as citation buttons. */
function remarkCitations() {
  return (tree: Root) => visit(tree)
}

function visit(node: Parent) {
  const children: RootContent[] = []
  for (const child of node.children as RootContent[]) {
    if (child.type === 'text') {
      children.push(...splitMarkers(child.value))
    } else {
      if ('children' in child && child.type !== 'link' && child.type !== 'linkReference') {
        visit(child as Parent)
      }
      children.push(child)
    }
  }
  node.children = children as Parent['children']
}

function splitMarkers(value: string): PhrasingContent[] {
  const parts: PhrasingContent[] = []
  let last = 0
  for (const match of value.matchAll(MARKER)) {
    const start = match.index ?? 0
    if (start > last) parts.push({ type: 'text', value: value.slice(last, start) })
    parts.push({
      type: 'link',
      url: `${CITE_PREFIX}${match[1]}`,
      children: [{ type: 'text', value: `S${match[1]}` }],
    })
    last = start + match[0].length
  }
  if (last < value.length) parts.push({ type: 'text', value: value.slice(last) })
  return parts.length ? parts : [{ type: 'text', value }]
}

interface CitationHandlers {
  onCite: (index: number) => void
  knownSources: Set<number>
}

const CitationContext = createContext<CitationHandlers>({
  onCite: () => {},
  knownSources: new Set(),
})

function MarkdownLink({ href, children }: ComponentPropsWithoutRef<'a'>) {
  const { onCite, knownSources } = useContext(CitationContext)
  if (href?.startsWith(CITE_PREFIX)) {
    const index = Number(href.slice(CITE_PREFIX.length))
    return (
      <button
        type="button"
        onClick={() => onCite(index)}
        disabled={!knownSources.has(index)}
        aria-label={`Show source ${index}`}
        className="mx-0.5 inline-flex items-center rounded bg-indigo-50 px-1.5 py-0 align-baseline text-xs font-semibold text-indigo-700 hover:bg-indigo-100 focus-visible:outline-2 focus-visible:outline-indigo-600 disabled:bg-slate-100 disabled:text-slate-500"
      >
        {children}
      </button>
    )
  }
  if (!href) return <span>{children}</span>
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      className="text-indigo-700 underline hover:text-indigo-900"
    >
      {children}
    </a>
  )
}

function MarkdownImage({ alt }: ComponentPropsWithoutRef<'img'>) {
  return <span className="text-slate-500">[{alt || 'image'}]</span>
}

function MarkdownTable({ children }: ComponentPropsWithoutRef<'table'>) {
  return (
    <div className="my-2 overflow-x-auto">
      <table className="min-w-full border-collapse text-sm">{children}</table>
    </div>
  )
}

function MarkdownTh({ children }: ComponentPropsWithoutRef<'th'>) {
  return <th className="border border-slate-200 bg-slate-50 px-2 py-1 text-left">{children}</th>
}

function MarkdownTd({ children }: ComponentPropsWithoutRef<'td'>) {
  return <td className="border border-slate-200 px-2 py-1 align-top">{children}</td>
}

const COMPONENTS: Components = {
  a: MarkdownLink,
  img: MarkdownImage,
  table: MarkdownTable,
  th: MarkdownTh,
  td: MarkdownTd,
}

const PLUGINS = [remarkGfm, remarkCitations]

interface Props extends CitationHandlers {
  text: string
}

/**
 * Renders an answer as Markdown. Raw HTML in the text is never rendered (it is skipped),
 * unsafe link protocols are removed by react-markdown's default URL filter, and images are
 * shown as their alt text so answers cannot load remote content.
 */
export default function AnswerMarkdown({ text, onCite, knownSources }: Props) {
  return (
    <CitationContext value={{ onCite, knownSources }}>
      <div className="answer-markdown space-y-2 text-sm leading-relaxed break-words text-slate-800 [&_code]:rounded [&_code]:bg-slate-100 [&_code]:px-1 [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold [&_ol]:list-decimal [&_ol]:pl-5 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-slate-900 [&_pre]:p-3 [&_pre]:text-slate-100 [&_pre_code]:bg-transparent [&_ul]:list-disc [&_ul]:pl-5">
        <Markdown remarkPlugins={PLUGINS} components={COMPONENTS} skipHtml>
          {text}
        </Markdown>
      </div>
    </CitationContext>
  )
}
