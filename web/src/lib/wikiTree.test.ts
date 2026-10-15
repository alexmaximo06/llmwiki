import { describe, expect, it } from 'vitest'
import { enrichTreeWithProgress, flattenLessons, flattenPages, resolveWikiHref } from './wikiTree'
import type { DocumentListItem, WikiNode } from '@/lib/types'

const tree: WikiNode[] = [
  { title: 'Overview', path: 'overview.md' },
  {
    title: 'Concepts',
    path: 'concepts.md',
    children: [
      { title: 'Attention', path: 'concepts/attention.md' },
      { title: 'Embeddings', path: 'concepts/embeddings.md' },
    ],
  },
  { title: 'Log', path: 'log.md' },
]

function lessonDoc(relativePath: string, status?: string): DocumentListItem {
  const parts = relativePath.split('/')
  const filename = parts.pop()!
  const dir = parts.length ? `/wiki/${parts.join('/')}/` : '/wiki/'
  return {
    id: relativePath,
    path: dir,
    filename,
    metadata: status ? { course: { status } } : {},
  } as unknown as DocumentListItem
}

describe('flattenPages', () => {
  it('returns every path-bearing node in tree order, structural pages included', () => {
    expect(flattenPages(tree).map((n) => n.path)).toEqual([
      'overview.md',
      'concepts.md',
      'concepts/attention.md',
      'concepts/embeddings.md',
      'log.md',
    ])
  })
})

describe('flattenLessons', () => {
  it('excludes structural pages', () => {
    expect(flattenLessons(tree).map((n) => n.path)).toEqual([
      'concepts.md',
      'concepts/attention.md',
      'concepts/embeddings.md',
    ])
  })
})

describe('enrichTreeWithProgress', () => {
  it('locks each lesson until every earlier lesson is complete', () => {
    const docs = [
      lessonDoc('concepts.md', 'complete'),
      lessonDoc('concepts/attention.md'),
      lessonDoc('concepts/embeddings.md'),
    ]
    const lessons = flattenLessons(enrichTreeWithProgress(tree, docs))
    expect(lessons.map((l) => ({ path: l.path, locked: l.locked }))).toEqual([
      { path: 'concepts.md', locked: false },
      { path: 'concepts/attention.md', locked: false },
      { path: 'concepts/embeddings.md', locked: true },
    ])
  })

  it('never locks the first lesson', () => {
    const docs = [lessonDoc('concepts.md'), lessonDoc('concepts/attention.md')]
    const lessons = flattenLessons(enrichTreeWithProgress(tree, docs))
    expect(lessons[0].locked).toBe(false)
    expect(lessons[1].locked).toBe(true)
  })
})

describe('resolveWikiHref', () => {
  const known = new Set(['overview.md', 'concepts/attention.md'])

  it('climbs above the root without looping', () => {
    expect(resolveWikiHref('../sources/a.md', 'overview.md', known)).toBe('sources/a.md')
    expect(resolveWikiHref('../../x.md', 'concepts/attention.md', known)).toBe('x.md')
  })

  it('resolves sibling and nested relative links', () => {
    expect(resolveWikiHref('./scaling.md', 'concepts/attention.md', known)).toBe('concepts/scaling.md')
    expect(resolveWikiHref('../overview.md', 'concepts/attention.md', known)).toBe('overview.md')
  })

  it('handles wiki-absolute links, fragments and encoding', () => {
    expect(resolveWikiHref('/wiki/concepts/attention.md#scaled', 'overview.md', known)).toBe('concepts/attention.md')
    expect(resolveWikiHref('My%20Page.md', 'overview.md', known)).toBe('My Page.md')
    expect(resolveWikiHref('concepts/attention.md', 'overview.md', known)).toBe('concepts/attention.md')
  })
})
