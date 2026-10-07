<template>
  <section class="page" data-module="boosting_station">
    <header class="page-head">
      <div>
        <h2>升压站监视管理</h2>
        <p class="page-desc">
          母线状态严格按「运行 → 热备用 → 检修 → 停运」逐档流转；断路器状态与母线同源，
          未合闸不得送电；每次状态变更同步调度待处理指令。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记升压站</button>
        <button class="btn" type="button" @click="exportRows">导出升压站监视清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>升压站编号</span>
        <input v-model="keyword" placeholder="按升压站编号检索" />
      </label>
      <label class="filter-item">
        <span>母线状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="s in busStates" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>详情</th>
          <th>状态操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">查看详情</button>
          </td>
          <td class="row-actions">
            <button
              v-if="nextBusState(row[母线状态字段])"
              class="link"
              type="button"
              :disabled="busyId === row.id"
              @click="openBusState(row)"
            >
              母线转{{ nextBusState(row[母线状态字段]) }}
            </button>
            <button
              v-if="row[母线状态字段] === '热备用'"
              class="link"
              type="button"
              :disabled="busyId === row.id"
              @click="openEnergize(row)"
            >
              站内送电
            </button>
            <span v-if="!nextBusState(row[母线状态字段])" class="muted">停运末态</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无升压站监视数据，可先登记升压站</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条升压站监视记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-if="successMessage" class="success-text">{{ successMessage }}</span>
    </footer>

    <!-- 详情弹层：母线/断路器状态直接取列表同源数据，再请求明细兜底 -->
    <div v-if="detailRow" class="modal-mask" @click.self="closeDetail">
      <div class="modal">
        <h3>升压站详情 · {{ detailRow['升压站编号'] }}</h3>
        <dl class="detail-grid">
          <template v-for="column in columns" :key="column">
            <dt>{{ column }}</dt>
            <dd :class="{ 'state-pill': true }">{{ detailRow[column] ?? '—' }}</dd>
          </template>
        </dl>
        <p class="muted">断路器状态由母线状态同源推导，列表与本页始终一致。</p>
        <div class="modal-actions">
          <button class="btn" type="button" @click="closeDetail">关闭</button>
        </div>
      </div>
    </div>

    <!-- 母线状态回写弹层 -->
    <div v-if="busStateTarget" class="modal-mask" @click.self="closeBusState">
      <div class="modal">
        <h3>母线状态回写</h3>
        <p>
          当前「<strong>{{ busStateTarget.current }}</strong
          >」，目标「<strong>{{ busStateTarget.target }}</strong
          >」。仅允许逐档前进，跨档与回退会被服务端拦截。
        </p>
        <div class="modal-actions">
          <button class="btn" type="button" @click="closeBusState">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitBusState">
            {{ submitting ? '提交中…' : '确认回写' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 站内送电弹层：必须确认断路器已合闸 -->
    <div v-if="energizeTarget" class="modal-mask" @click.self="closeEnergize">
      <div class="modal">
        <h3>站内送电 · {{ energizeTarget['升压站编号'] }}</h3>
        <p>当前母线「热备用」、断路器「分闸」。送电前必须现场确认断路器已合闸。</p>
        <label class="check-line">
          <input v-model="breakerConfirmed" type="checkbox" />
          已现场确认断路器处于合闸状态
        </label>
        <p v-if="energizeHint" class="error-text">{{ energizeHint }}</p>
        <div class="modal-actions">
          <button class="btn" type="button" @click="closeEnergize">取消</button>
          <button
            class="btn primary"
            type="button"
            :disabled="submitting || !breakerConfirmed"
            @click="submitEnergize"
          >
            {{ submitting ? '提交中…' : '确认送电' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/boosting_station'
const columns = ['升压站编号', '进线电压', '出线电压', '主变容量', '母线状态', '断路器状态', '无功补偿', '运行状态']
const busStates = ['运行', '热备用', '检修', '停运']
const 母线状态字段 = '母线状态'

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const busyId = ref<number | null>(null)

// 详情、回写、送电弹层状态
const detailRow = ref<Row | null>(null)
const busStateTarget = ref<{ id: number; current: string; target: string } | null>(null)
const energizeTarget = ref<Row | null>(null)
const breakerConfirmed = ref(false)
const energizeHint = ref('')
const submitting = ref(false)

const stats = computed(() => {
  const running = rows.value.filter((r) => r[母线状态字段] === '运行').length
  const hot = rows.value.filter((r) => r[母线状态字段] === '热备用').length
  const down = rows.value.filter((r) => r[母线状态字段] === '停运').length
  return [
    { label: '运行中站点', value: running },
    { label: '热备用站点', value: hot },
    { label: '停运站点', value: down },
  ]
})

// 母线只能逐档前进：返回该状态允许写入的下一档；末态返回 null
function nextBusState(current: unknown): string | null {
  const idx = busStates.indexOf(String(current))
  if (idx < 0 || idx >= busStates.length - 1) return null
  return busStates[idx + 1]
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '升压站登记入口尚未接入审批流'
}

// --------------------------------------------------------------- 详情
async function openDetail(row: Row) {
  errorMessage.value = ''
  detailRow.value = { ...row }
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (response.ok) {
      // 以服务端明细为准（与列表同源服务，正常情况下逐字段一致）
      detailRow.value = await response.json()
      // 同步回列表行，保证两侧永不错位
      const idx = rows.value.findIndex((r) => String(r.id) === String(row.id))
      if (idx >= 0) rows.value[idx] = { ...detailRow.value }
    }
  } catch {
    // 明细请求失败时保留列表数据兜底，不清空弹层
  }
}

function closeDetail() {
  detailRow.value = null
}

// --------------------------------------------------------------- 母线回写
function openBusState(row: Row) {
  const current = String(row[母线状态字段] ?? '')
  const target = nextBusState(current)
  if (!target) {
    errorMessage.value = '该站已处于停运末态，不能再改档'
    return
  }
  errorMessage.value = ''
  busStateTarget.value = { id: Number(row.id), current, target }
}

function closeBusState() {
  busStateTarget.value = null
  submitting.value = false
}

async function submitBusState() {
  if (!busStateTarget.value) return
  submitting.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${busStateTarget.value.id}/bus-state`, {
      method: 'POST',
      body: JSON.stringify({ values: { target: busStateTarget.value.target } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      errorMessage.value = payload.message || '母线状态回写被拒绝'
      return
    }
    successMessage.value = payload.message
    closeBusState()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '母线状态回写失败'
  } finally {
    submitting.value = false
  }
}

// --------------------------------------------------------------- 站内送电
function openEnergize(row: Row) {
  energizeTarget.value = row
  breakerConfirmed.value = false
  energizeHint.value = ''
  errorMessage.value = ''
}

function closeEnergize() {
  energizeTarget.value = null
  breakerConfirmed.value = false
  energizeHint.value = ''
  submitting.value = false
}

async function submitEnergize() {
  if (!energizeTarget.value) return
  if (!breakerConfirmed.value) {
    energizeHint.value = '请先确认断路器已合闸'
    return
  }
  const id = Number(energizeTarget.value.id)
  busyId.value = id
  submitting.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    // 前端也做一次在途防抖：同一站送电请求未结束前禁二次点击（按钮已 disable），
    // 真正的「重复提交只落一条」由服务端按待执行调度单判定
    const response = await request(`${ENDPOINT}/${id}/energize`, {
      method: 'POST',
      body: JSON.stringify({ values: { breaker_closed: true } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      errorMessage.value = payload.message || '送电失败'
      return
    }
    successMessage.value = payload.message
    closeEnergize()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '站内送电请求失败'
  } finally {
    busyId.value = null
    submitting.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (keyword.value) params.set('keyword', keyword.value)
  if (statusFilter.value) params.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('升压站列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '升压站监视列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.muted {
  color: #8a94a6;
  font-size: 12px;
}

.success-text {
  color: #1a8a4c;
}

.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal {
  background: #fff;
  border-radius: 10px;
  padding: 24px;
  width: 520px;
  max-width: calc(100vw - 48px);
  box-shadow: 0 18px 48px rgba(15, 23, 42, 0.25);
}

.modal h3 {
  margin: 0 0 12px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 20px;
}

.detail-grid {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 8px 16px;
  margin: 0;
}

.detail-grid dt {
  color: #64748b;
}

.detail-grid dd {
  margin: 0;
}

.check-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0;
}

button[disabled] {
  opacity: 0.55;
  cursor: not-allowed;
}
</style>
