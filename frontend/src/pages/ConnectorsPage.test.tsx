import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { NAV_ITEMS, pageTitle } from '../components/navigation'
import * as api from '../lib/api'
import { CONNECTOR_OPTIONS, makeConnector, makeConnectorDetail } from '../test/fixtures'
import ConnectorsPage from './ConnectorsPage'

function page(items: api.Connector[], total = items.length): api.Page<api.Connector> {
  return { items, total, limit: 20, offset: 0 }
}

function renderPage(options: api.ConnectorOptions = CONNECTOR_OPTIONS) {
  vi.spyOn(api, 'fetchConnectorOptions').mockResolvedValue(options)
  return render(
    <MemoryRouter initialEntries={['/connectors']}>
      <Routes>
        <Route path="/connectors" element={<ConnectorsPage />} />
        <Route path="/connectors/:connectorId" element={<p>Connector detail page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ConnectorsPage', () => {
  it('is in the navigation', () => {
    expect(NAV_ITEMS.find((item) => item.to === '/connectors')?.label).toBe('Connectors')
    expect(pageTitle('/connectors')).toBe('Connectors')
    expect(pageTitle('/connectors/abc')).toBe('Connector details')
  })

  it('lists connectors with source, status, last success, documents and errors', async () => {
    vi.spyOn(api, 'listConnectors').mockResolvedValue(
      page([
        makeConnector(),
        makeConnector({
          id: 'conn-2',
          name: 'Policies',
          bucket: 'shared',
          prefix: 'policies/',
          uri: 's3://shared/policies/',
          enabled: false,
          last_sync_status: 'failed',
          last_success_at: null,
          last_error_code: 'access_denied',
          last_error_message: 'Access to the bucket was denied.',
          document_count: 0,
        }),
      ]),
    )
    renderPage()

    const table = await screen.findByRole('table', { name: 'Connectors' })
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(within(rows[1]).getByRole('link', { name: 'Team handbook' })).toHaveAttribute(
      'href',
      '/connectors/conn-1',
    )
    expect(within(rows[1]).getByText('S3')).toBeInTheDocument()
    expect(within(rows[1]).getByText('s3://team-docs/handbook/')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Every hour')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Synced')).toBeInTheDocument()
    expect(within(rows[1]).getByText('10')).toBeInTheDocument()
    expect(within(rows[2]).getByText('Manual only')).toBeInTheDocument()
    expect(within(rows[2]).getByText('Failed')).toBeInTheDocument()
    expect(within(rows[2]).getByText('Access to the bucket was denied.')).toBeInTheDocument()
    expect(within(rows[2]).getByText('—')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Configured connectors/ })).toHaveTextContent('(2)')
  })

  it('shows loading, then an empty state', async () => {
    vi.spyOn(api, 'listConnectors').mockResolvedValue(page([]))
    renderPage()
    expect(screen.getByText('Loading connectors…')).toBeInTheDocument()
    expect(await screen.findByText('No connectors yet')).toBeInTheDocument()
  })

  it('shows an error state with a retry button', async () => {
    const list = vi
      .spyOn(api, 'listConnectors')
      .mockRejectedValueOnce(new api.ApiError(503, 'service_unavailable', 'Database unavailable'))
      .mockResolvedValueOnce(page([makeConnector()]))
    renderPage()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Connectors could not be loaded.')
    expect(alert).toHaveTextContent('Database unavailable')
    await userEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
    expect(await screen.findAllByRole('link', { name: 'Team handbook' })).toHaveLength(2)
    expect(list).toHaveBeenCalledTimes(2)
  })

  it('explains backend configuration when connectors are not configured', async () => {
    vi.spyOn(api, 'listConnectors').mockResolvedValue(page([]))
    renderPage({ ...CONNECTOR_OPTIONS, configured: false, allowed_sources: [] })
    expect(
      await screen.findByText('S3 connectors are not configured on the backend.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add connector' })).not.toBeInTheDocument()
  })

  it('validates the form before sending and never asks for credentials', async () => {
    vi.spyOn(api, 'listConnectors').mockResolvedValue(page([]))
    const create = vi.spyOn(api, 'createConnector')
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Add connector' }))

    expect(screen.getByText(/AWS credentials are configured on the backend/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/secret|access key|endpoint/i)).not.toBeInTheDocument()
    expect(screen.getByText(/team-docs, shared\/policies\//)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Create connector' }))
    expect(screen.getByLabelText('Name')).toHaveAccessibleDescription(/Enter a name/)
    expect(screen.getByLabelText('S3 bucket')).toHaveAttribute('aria-invalid', 'true')

    await userEvent.type(screen.getByLabelText('Name'), 'Handbook')
    await userEvent.type(screen.getByLabelText('S3 bucket'), 'Bad_Bucket')
    await userEvent.type(screen.getByLabelText(/Prefix/), 'a/../b')
    await userEvent.click(screen.getByRole('button', { name: 'Create connector' }))
    expect(screen.getByText(/3–63 lowercase letters/)).toBeInTheDocument()
    expect(screen.getByText(/must not contain empty/)).toBeInTheDocument()

    await userEvent.clear(screen.getByLabelText('S3 bucket'))
    await userEvent.type(screen.getByLabelText('S3 bucket'), 'other-bucket')
    await userEvent.clear(screen.getByLabelText(/Prefix/))
    await userEvent.click(screen.getByRole('button', { name: 'Create connector' }))
    expect(screen.getByText(/not in the server’s allowed sources/)).toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
  })

  it('creates a connector and opens it', async () => {
    vi.spyOn(api, 'listConnectors').mockResolvedValue(page([]))
    const create = vi.spyOn(api, 'createConnector').mockResolvedValue(makeConnectorDetail())
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Add connector' }))
    await userEvent.type(screen.getByLabelText('Name'), '  Team handbook ')
    await userEvent.type(screen.getByLabelText('S3 bucket'), 'team-docs')
    await userEvent.type(screen.getByLabelText(/Prefix/), 'handbook')
    await userEvent.selectOptions(screen.getByLabelText('Automatic sync'), 'Every day')
    await userEvent.click(screen.getByLabelText(/Enable automatic syncs/))
    await userEvent.click(screen.getByRole('button', { name: 'Create connector' }))

    expect(create).toHaveBeenCalledWith({
      name: 'Team handbook',
      bucket: 'team-docs',
      prefix: 'handbook/',
      sync_interval_minutes: 1440,
      enabled: false,
    })
    expect(await screen.findByText('Connector detail page')).toBeInTheDocument()
  })

  it('shows server validation errors on the matching field', async () => {
    vi.spyOn(api, 'listConnectors').mockResolvedValue(page([]))
    vi.spyOn(api, 'createConnector').mockRejectedValue(
      new api.ApiError(409, 'connector_name_taken', 'A connector with this name already exists.'),
    )
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Add connector' }))
    await userEvent.type(screen.getByLabelText('Name'), 'Handbook')
    await userEvent.type(screen.getByLabelText('S3 bucket'), 'team-docs')
    await userEvent.click(screen.getByRole('button', { name: 'Create connector' }))
    expect(await screen.findByText('A connector with this name already exists.')).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true')
  })

  it('polls only while a sync is active', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const list = vi
        .spyOn(api, 'listConnectors')
        .mockResolvedValueOnce(page([makeConnector({ last_sync_status: 'running' })]))
        .mockResolvedValue(page([makeConnector()]))
      renderPage()
      expect(await screen.findAllByText('Syncing')).toHaveLength(2) // table and mobile list
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500)
      })
      expect(await screen.findAllByText('Synced')).toHaveLength(2)
      expect(list).toHaveBeenCalledTimes(2)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000)
      })
      expect(list).toHaveBeenCalledTimes(2) // idle: no more requests
    } finally {
      vi.useRealTimers()
    }
  })
})
