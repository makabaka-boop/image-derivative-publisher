import React, { useCallback, useEffect, useRef, useState } from 'react'

const MAX_BYTES = 4 * 1024 * 1024
const TERMINAL = new Set(['succeeded', 'failed', 'canceled'])

async function api(path, options = {}) {
  const res = await fetch(path, options)
  let body = null
  try {
    body = await res.json()
  } catch {
    /* non-JSON error body */
  }
  if (!res.ok) {
    const detail = body && body.detail ? body.detail : `HTTP ${res.status}`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return body
}

export default function App() {
  const [image, setImage] = useState(null)
  const [jobs, setJobs] = useState([])
  const [crop, setCrop] = useState(null) // natural pixels {x,y,w,h}
  const [selRect, setSelRect] = useState(null) // displayed pixels {x,y,w,h}
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [uploading, setUploading] = useState(false)

  const stageRef = useRef(null)
  const imgRef = useRef(null)
  const dragRef = useRef(null)

  // ---- polling: the page renders ONLY server-confirmed state -------------
  const refreshImage = useCallback(async (id) => {
    try {
      setImage(await api(`/api/images/${id}`))
    } catch (err) {
      setError(err.message)
    }
  }, [])

  const refreshJobs = useCallback(async (id) => {
    try {
      const body = await api(`/api/images/${id}/jobs`)
      setJobs(body.jobs)
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    if (!image) return undefined
    const id = image.id
    const t1 = setInterval(() => refreshImage(id), 1500)
    const t2 = setInterval(() => refreshJobs(id), 2000)
    return () => {
      clearInterval(t1)
      clearInterval(t2)
    }
  }, [image?.id, refreshImage, refreshJobs])

  // ---- upload -------------------------------------------------------------
  async function onFileChosen(event) {
    const file = event.target.files && event.target.files[0]
    event.target.value = ''
    if (!file) return
    setError(null)
    setNotice(null)
    if (!['image/png', 'image/jpeg'].includes(file.type)) {
      setError('仅支持 PNG / JPEG 图片')
      return
    }
    if (file.size > MAX_BYTES) {
      setError(`文件超过 4 MiB 限制（${(file.size / 1048576).toFixed(2)} MiB）`)
      return
    }
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const uploaded = await api('/api/images', { method: 'POST', body: form })
      setImage(uploaded)
      setCrop(null)
      setSelRect(null)
      setJobs([])
      setNotice(
        uploaded.created
          ? `已上传：${uploaded.width}×${uploaded.height}`
          : `相同内容已存在，复用图片 #${uploaded.id}`,
      )
      refreshJobs(uploaded.id)
    } catch (err) {
      setError(`上传失败：${err.message}`)
    } finally {
      setUploading(false)
    }
  }

  // ---- crop selection (mouse drag on the stage) ---------------------------
  function stagePoint(event) {
    const box = stageRef.current.getBoundingClientRect()
    return {
      x: event.clientX - box.left,
      y: event.clientY - box.top,
      w: box.width,
      h: box.height,
    }
  }

  function onMouseDown(event) {
    if (!image) return
    event.preventDefault()
    const p = stagePoint(event)
    dragRef.current = { x0: p.x, y0: p.y }
    setSelRect({ x: p.x, y: p.y, w: 0, h: 0 })
  }

  function onMouseMove(event) {
    const drag = dragRef.current
    if (!drag) return
    const p = stagePoint(event)
    const x = Math.max(0, Math.min(drag.x0, p.x))
    const y = Math.max(0, Math.min(drag.y0, p.y))
    const x2 = Math.min(p.w, Math.max(drag.x0, p.x))
    const y2 = Math.min(p.h, Math.max(drag.y0, p.y))
    setSelRect({ x, y, w: x2 - x, h: y2 - y })
  }

  function onMouseUp() {
    const drag = dragRef.current
    dragRef.current = null
    if (!drag || !selRect || !imgRef.current) return
    const img = imgRef.current
    const scaleX = img.naturalWidth / img.clientWidth
    const scaleY = img.naturalHeight / img.clientHeight
    const natural = {
      x: Math.round(selRect.x * scaleX),
      y: Math.round(selRect.y * scaleY),
      w: Math.round(selRect.w * scaleX),
      h: Math.round(selRect.h * scaleY),
    }
    natural.x = Math.max(0, Math.min(natural.x, img.naturalWidth - 1))
    natural.y = Math.max(0, Math.min(natural.y, img.naturalHeight - 1))
    natural.w = Math.max(0, Math.min(natural.w, img.naturalWidth - natural.x))
    natural.h = Math.max(0, Math.min(natural.h, img.naturalHeight - natural.y))
    setCrop(natural.w >= 1 && natural.h >= 1 ? natural : null)
  }

  // ---- job actions ----------------------------------------------------------
  async function requestDerivatives() {
    if (!image || !crop) return
    setError(null)
    try {
      const body = await api(`/api/images/${image.id}/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(crop),
      })
      setNotice(
        body.reused
          ? `相同裁切参数已存在（作业 #${body.job.id}，状态 ${body.job.status}），直接复用`
          : `已创建作业 #${body.job.id}（版本 v${body.job.version}）`,
      )
      refreshJobs(image.id)
      refreshImage(image.id)
    } catch (err) {
      setError(`申请失败：${err.message}`)
    }
  }

  async function cancelJob(jobId) {
    setError(null)
    try {
      await api(`/api/jobs/${jobId}/cancel`, { method: 'POST' })
      refreshJobs(image.id)
    } catch (err) {
      setError(`取消失败：${err.message}`)
    }
  }

  const published = image && image.published

  return (
    <div className="page">
      <h1>WebP 派生图服务</h1>
      <p className="hint">
        上传不超过 4 MiB 的 PNG/JPEG，框选裁切区域，申请 256 / 512 像素 WebP 派生图。
        页面仅展示服务端确认发布的版本。
      </p>

      <div className="toolbar">
        <label className="upload-btn">
          {uploading ? '上传中…' : '选择图片'}
          <input
            type="file"
            accept="image/png,image/jpeg"
            onChange={onFileChosen}
            disabled={uploading}
            hidden
          />
        </label>
        {image && (
          <span className="dim">
            图片 #{image.id}：{image.width}×{image.height}，已发布版本 v{image.published_version}
          </span>
        )}
      </div>

      {error && <div className="alert error">{error}</div>}
      {notice && <div className="alert ok">{notice}</div>}

      {image && (
        <div className="columns">
          <section>
            <h2>裁切</h2>
            <div
              ref={stageRef}
              className="stage"
              onMouseDown={onMouseDown}
              onMouseMove={onMouseMove}
              onMouseUp={onMouseUp}
              onMouseLeave={onMouseUp}
            >
              <img ref={imgRef} src={image.original_url} alt="original" draggable="false" />
              {selRect && selRect.w > 0 && selRect.h > 0 && (
                <div
                  className="selection"
                  style={{
                    left: selRect.x,
                    top: selRect.y,
                    width: selRect.w,
                    height: selRect.h,
                  }}
                />
              )}
            </div>
            <div className="crop-row">
              <span>
                {crop
                  ? `裁切框：x=${crop.x} y=${crop.y} w=${crop.w} h=${crop.h}`
                  : '在图片上拖拽以选择裁切框'}
              </span>
              <button onClick={requestDerivatives} disabled={!crop}>
                申请 256/512 WebP 派生图
              </button>
            </div>
          </section>

          <section>
            <h2>已发布版本（服务端确认）</h2>
            {published ? (
              <div className="published">
                <p>
                  作业 #{published.id} · 版本 v{published.version} · 裁切 (
                  {published.crop.x},{published.crop.y} {published.crop.w}×{published.crop.h})
                </p>
                <div className="derivatives">
                  {Object.entries(published.derivative_urls || {}).map(([size, url]) => (
                    <figure key={size}>
                      <img src={`${url}?v=${published.version}`} alt={`${size}px`} />
                      <figcaption>{size}px WebP</figcaption>
                    </figure>
                  ))}
                </div>
              </div>
            ) : (
              <p className="hint">尚无已发布的派生图。</p>
            )}

            <h2>作业历史</h2>
            {jobs.length === 0 ? (
              <p className="hint">暂无作业。</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>版本</th>
                    <th>裁切</th>
                    <th>状态</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((job) => (
                    <tr key={job.id} className={job.published ? 'published-row' : ''}>
                      <td>{job.id}</td>
                      <td>v{job.version}</td>
                      <td>
                        ({job.crop.x},{job.crop.y} {job.crop.w}×{job.crop.h})
                      </td>
                      <td>
                        {job.status}
                        {job.published && ' · 已发布'}
                        {job.error && <span className="err-text" title={job.error}> ⚠</span>}
                      </td>
                      <td>
                        {!TERMINAL.has(job.status) && (
                          <button className="link" onClick={() => cancelJob(job.id)}>
                            取消
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
