import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import { useUserStore } from '@/stores'
import type { DocumentListItem } from '@/lib/types'
import { useKBDocuments } from './useKBDocuments'

vi.mock('@/lib/api', () => ({
  apiFetch: vi.fn(),
  getDocumentsWsUrl: vi.fn(() => 'ws://example.test/documents'),
}))

vi.mock('@/lib/auth-token', () => ({
  refreshAccessToken: vi.fn(),
}))

const apiFetchMock = vi.mocked(apiFetch)

class FakeWebSocket {
  onopen: (() => void) | null = null
  onmessage: (() => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null

  send = vi.fn()
  close = vi.fn()
}

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

function documentItem(id: string, overrides: Partial<DocumentListItem> = {}): DocumentListItem {
  return {
    id,
    knowledge_base_id: 'kb-1',
    user_id: 'user-1',
    filename: `${id}.md`,
    title: id,
    path: `/wiki/${id}.md`,
    file_type: 'wiki',
    file_size: 100,
    status: 'completed',
    page_count: null,
    tags: [],
    date: null,
    metadata: null,
    error_message: null,
    url: null,
    version: 1,
    document_number: null,
    sort_order: null,
    archived: false,
    created_at: '2026-08-10T00:00:00.000Z',
    updated_at: '2026-08-10T00:00:00.000Z',
    ...overrides,
  }
}

beforeEach(() => {
  apiFetchMock.mockReset()
  vi.stubGlobal('WebSocket', FakeWebSocket)
  useUserStore.setState({ accessToken: 'token-1' })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('useKBDocuments', () => {
  it('keeps the current wiki visible while a refreshed token revalidates documents', async () => {
    const originalDocument = documentItem('overview')
    apiFetchMock.mockResolvedValueOnce([originalDocument])

    const { result } = renderHook(() => useKBDocuments('kb-1'))

    await waitFor(() => expect(result.current.loading).toBe(false))
    const originalDocuments = result.current.documents
    expect(originalDocuments).toEqual([originalDocument])

    const refresh = deferred<DocumentListItem[]>()
    apiFetchMock.mockReturnValueOnce(refresh.promise)

    act(() => {
      useUserStore.setState({ accessToken: 'token-2' })
    })

    expect(result.current.loading).toBe(false)
    expect(result.current.documents).toBe(originalDocuments)

    await act(async () => {
      refresh.resolve([
        documentItem('overview', { updated_at: '2026-08-10T01:00:00.000Z' }),
      ])
      await refresh.promise
    })

    expect(result.current.loading).toBe(false)
    expect(result.current.documents).toBe(originalDocuments)
    expect(apiFetchMock).toHaveBeenLastCalledWith(
      '/v1/knowledge-bases/kb-1/documents',
      'token-2',
    )
  })

  it('still blocks and clears stale documents when switching to another wiki', async () => {
    apiFetchMock.mockResolvedValueOnce([documentItem('overview')])
    const nextWiki = deferred<DocumentListItem[]>()

    const { result, rerender } = renderHook(
      ({ knowledgeBaseId }) => useKBDocuments(knowledgeBaseId),
      { initialProps: { knowledgeBaseId: 'kb-1' } },
    )

    await waitFor(() => expect(result.current.loading).toBe(false))
    apiFetchMock.mockReturnValueOnce(nextWiki.promise)

    rerender({ knowledgeBaseId: 'kb-2' })

    expect(result.current.loading).toBe(true)
    expect(result.current.documents).toEqual([])

    await act(async () => {
      nextWiki.resolve([
        documentItem('start', { knowledge_base_id: 'kb-2' }),
      ])
      await nextWiki.promise
    })

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.documents[0].knowledge_base_id).toBe('kb-2')
  })

  it('reports a failed load instead of looking like an empty wiki, and recovers on retry', async () => {
    apiFetchMock.mockRejectedValueOnce(new Error('503 Service Unavailable'))
    const { result } = renderHook(() => useKBDocuments('kb-1'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.loadError).toBe('503 Service Unavailable')
    expect(result.current.documents).toEqual([])

    apiFetchMock.mockResolvedValueOnce([documentItem('overview')])
    await act(async () => {
      result.current.refetchDocuments()
    })

    await waitFor(() => expect(result.current.loadError).toBeNull())
    expect(result.current.documents).toHaveLength(1)
  })
})
