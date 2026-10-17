import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import * as api from '../lib/api'
import { CONNECTOR_OPTIONS, makeConnectorDetail, makeRun } from '../test/fixtures'
import ConnectorDetailPage from './ConnectorDetailPage'

function history(items: api.SyncRun[]): api.Page<api.SyncRun> {
  return { items, total: items.length, limit: 10, offset: 0 }
}

function renderDetail(
  detail: api.ConnectorDetail | Error,
  runs: api.SyncRun[] = [makeRun()],
) {
  vi.spyOn(api, 'fetchConnectorOptions').mockResolvedValue(CONNECTOR_OPTIONS)
  const get = vi.spyOn(api, 'getConnector')
  if (detail instanceof Error) get.mockRejectedValue(detail)
  else get.mockResolvedValue(detail)
  vi.spyOn(api, 'listSyncRuns').mockResolvedValue(history(runs))
  render(
    <MemoryRouter initialEntries={['/connectors/conn-1']}>
      <Routes>
        <Route path="/connectors/:connectorId" element={<ConnectorDetailPage />} />
        <Route path="/connectors" element={<p>Connector list page</p>} />
      </Routes>
    </MemoryRouter>,
  )
  return get
}

describe('ConnectorDetailPage', () => {
  it('shows configuration, the last sync counts and history', async () => {
    renderDetail(makeConnectorDetail(), [
      makeRun(),
      makeRun({
        id: 'run-0',
        status: 'failed',
        trigger: 'scheduled',
        attempts: 3,
        error_code: 's3_unavailable',
        error_message: 'Amazon S3 is throttling or temporarily unavailable.',
      }),
    ])
    expect(await screen.findByRole('heading', { name: 'Team handbook' })).toBeInTheDocument()
    expect(screen.getAllByText('s3://team-docs/handbook/').length).toBeGreaterThan(0)
    expect(screen.getByText('10 documents')).toBeInTheDocument()

    const config = screen.getByRole('region', { name: 'Configuration' })
    expect(within(config).getByText('Amazon S3')).toBeInTheDocument()
    expect(within(config).getByText('team-docs')).toBeInTheDocument()
    expect(within(config).getByText('handbook/')).toBeInTheDocument()
    expect(within(config).getByText('Every hour')).toBeInTheDocument()

    const last = screen.getByRole('region', { name: 'Last sync' })
    for (const [label, value] of [
      ['Imported', '3'],
      ['Updated', '1'],
      ['Unchanged', '6'],
      ['Removed', '1'],
      ['Skipped', '1'],
      ['Failed', '0'],
    ]) {
      expect(within(last).getByText(label).nextSibling).toHaveTextContent(value)
    }
    expect(within(last).getByText(/12 objects listed/)).toBeInTheDocument()

    const runs = await screen.findByRole('region', { name: 'Sync history' })
    const items = within(runs).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(within(items[0]).getByText(/Manual · 30 s/)).toBeInTheDocument()
    expect(within(items[1]).getByText(/Scheduled/)).toHaveTextContent('3 attempts')
    expect(within(items[1]).getByText(/throttling/)).toHaveTextContent('(s3_unavailable)')
  })

  it('expands a run to show failed and skipped objects', async () => {
    renderDetail(makeConnectorDetail())
    vi.spyOn(api, 'getSyncRun').mockResolvedValue({
      ...makeRun({ status: 'partial', objects_failed: 1 }),
      failures: [
        { key: 'handbook/big.pdf', code: 'object_too_large', message: 'Too large.', kind: 'failed' },
        { key: 'handbook/logo.png', code: 'unsupported_file_type', message: 'Not imported.', kind: 'skipped' },
      ],
    })
    await userEvent.click(await screen.findByRole('button', { name: 'Details' }))
    expect(await screen.findByText('handbook/big.pdf')).toBeInTheDocument()
    expect(screen.getByText('handbook/logo.png')).toBeInTheDocument()
    expect(screen.getByText(/\(object_too_large\)/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide details' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('queues a manual sync and polls while it runs', async () => {
    const get = renderDetail(makeConnectorDetail())
    const sync = vi.spyOn(api, 'requestSync').mockResolvedValue({
      created: true,
      run: makeRun({ status: 'pending' }),
    })
    get.mockResolvedValue(
      makeConnectorDetail({
        last_sync_status: 'running',
        active_run: makeRun({ id: 'run-2', status: 'running', documents_imported: 1 }),
      }),
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))
    expect(sync).toHaveBeenCalledWith('conn-1')
    expect(await screen.findByRole('region', { name: 'Current sync' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Sync queued')
    expect(screen.getByRole('button', { name: 'Cancel sync' })).toBeEnabled()
  })

  it('cancels an active sync', async () => {
    renderDetail(
      makeConnectorDetail({
        last_sync_status: 'running',
        active_run: makeRun({ id: 'run-2', status: 'running' }),
      }),
    )
    const cancel = vi
      .spyOn(api, 'cancelSyncRun')
      .mockResolvedValue({ ...makeRun({ id: 'run-2', status: 'running', cancel_requested: true }), failures: [] })
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel sync' }))
    expect(cancel).toHaveBeenCalledWith('conn-1', 'run-2')
  })

  it('pauses and resumes automatic syncs', async () => {
    renderDetail(makeConnectorDetail())
    const update = vi
      .spyOn(api, 'updateConnector')
      .mockResolvedValue(makeConnectorDetail({ enabled: false }))
    await userEvent.click(await screen.findByRole('button', { name: 'Pause automatic syncs' }))
    expect(update).toHaveBeenCalledWith('conn-1', { enabled: false })
  })

  it('edits the name and interval but not the bucket or prefix', async () => {
    const get = renderDetail(makeConnectorDetail())
    const update = vi.spyOn(api, 'updateConnector').mockResolvedValue(makeConnectorDetail())
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    expect(screen.getByLabelText('S3 bucket')).toBeDisabled()
    expect(screen.getByLabelText(/Prefix/)).toBeDisabled()
    await userEvent.clear(screen.getByLabelText('Name'))
    await userEvent.type(screen.getByLabelText('Name'), 'Handbook (EN)')
    await userEvent.selectOptions(screen.getByLabelText('Automatic sync'), 'Every 15 minutes')
    get.mockClear()
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(update).toHaveBeenCalledWith('conn-1', {
      name: 'Handbook (EN)',
      sync_interval_minutes: 15,
      enabled: true,
    })
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()
    expect(get).toHaveBeenCalled()
  })

  it('deletes only after confirmation, explaining what is removed', async () => {
    renderDetail(makeConnectorDetail())
    const remove = vi.spyOn(api, 'deleteConnector').mockResolvedValue(undefined)
    await userEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('alertdialog', { name: 'Delete this connector?' })
    expect(dialog).toHaveTextContent('10 documents it imported')
    expect(dialog).toHaveTextContent('Uploaded documents are not affected')
    expect(dialog).toHaveTextContent('nothing in the S3 bucket is changed')

    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(remove).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await userEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', {
        name: 'Delete connector and documents',
      }),
    )
    expect(remove).toHaveBeenCalledWith('conn-1')
    expect(await screen.findByText('Connector list page')).toBeInTheDocument()
  })

  it('reports a failed sync and that nothing was deleted', async () => {
    renderDetail(
      makeConnectorDetail({
        last_sync_status: 'failed',
        last_error_code: 'credentials_missing',
        last_error_message: 'No usable AWS credentials are configured for the worker.',
        latest_run: makeRun({ status: 'failed', error_code: 'credentials_missing' }),
      }),
    )
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The last sync failed')
    expect(alert).toHaveTextContent('No usable AWS credentials')
    expect(alert).toHaveTextContent('code: credentials_missing')
    expect(alert).toHaveTextContent('Nothing was deleted')
  })

  it('shows action errors', async () => {
    renderDetail(makeConnectorDetail())
    vi.spyOn(api, 'requestSync').mockRejectedValue(
      new api.ApiError(404, 'connector_not_found', 'Connector not found.'),
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Connector not found.')
  })

  it('shows a not-found state', async () => {
    renderDetail(new api.ApiError(404, 'connector_not_found', 'Connector not found.'))
    expect(await screen.findByRole('alert')).toHaveTextContent('This connector does not exist.')
    expect(screen.getByRole('link', { name: 'All connectors' })).toHaveAttribute(
      'href',
      '/connectors',
    )
  })

  it('shows a connector that never synced', async () => {
    renderDetail(makeConnectorDetail({ last_sync_status: null, latest_run: null }), [])
    expect(await screen.findByText('This connector has not synced yet.')).toBeInTheDocument()
    expect(await screen.findByText('No syncs yet.')).toBeInTheDocument()
    expect(screen.getAllByText('Never synced').length).toBeGreaterThan(0)
  })
})
