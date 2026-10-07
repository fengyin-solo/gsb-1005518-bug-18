<template>
  <section class="page" data-module="boosting_station">
    <header class="page-head">
      <div>
        <h2>升压站监视管理</h2>
        <p class="page-desc">母线状态严格按 运行 → 热备用 → 检修 → 停运 单向逐档流转；断路器状态与母线同源，停运站只能申请站内送电。</p>
      </div>
      <div class="page-actions">
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
          <option value="">全部状态</option>
          <option v-for="state in busStates" :key="state" :value="state">{{ state }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
          <th>详情</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <template v-if="nextAction(row['母线状态'])">
              <button class="link" type="button" @click="runAction(nextAction(row['母线状态']) as string, row)">
                {{ nextAction(row['母线状态']) }}
              </button>
            </template>
            <template v-else>
              <button class="link" type="button" @click="requestPowerOn(row)">申请站内送电</button>
            </template>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">查看详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无升压站监视数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条升压站监视记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="infoMessage" class="info-text">{{ infoMessage }}</span>
    </footer>

    <div v-if="detail" class="drawer-mask" @click.self="closeDetail">
      <aside class="drawer">
        <header class="drawer-head">
          <h3>{{ detail['升压站编号'] }} 详情</h3>
          <button class="btn ghost" type="button" @click="closeDetail">关闭</button>
        </header>
        <dl class="detail-list">
          <div v-for="column in columns" :key="column">
            <dt>{{ column }}</dt>
            <dd>{{ detail[column] ?? '—' }}</dd>
          </div>
        </dl>
        <p class="detail-tip">详情与列表读取同一份状态回写结果：母线状态与断路器状态必须一致，对不上时请刷新。</p>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/boosting_station'
const columns = ['升压站编号', '进线电压', '出线电压', '主变容量', '母线状态', '断路器状态', '无功补偿', '运行状态']
const busStates = ['运行', '热备用', '检修', '停运']
// 母线只能逐档前进：当前档位决定列表里唯一放行的动作；停运档无后继，只能申请站内送电。
const NEXT_ACTIONS: Record<string, string> = {
  '运行': '转热备用',
  '热备用': '转检修',
  '检修': '转停运',
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const infoMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const detail = ref<Row | null>(null)
const stats = ref([
  { label: '运行中站点', value: 0 },
  { label: '停运站点', value: 0 },
  { label: '待调度确认', value: 0 },
])

function nextAction(busState: string | number | null): string | undefined {
  return NEXT_ACTIONS[String(busState ?? '')]
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function readResult(response: Response, fallback: string): Promise<{ ok: boolean; message: string }> {
  try {
    const payload = await response.json()
    if (typeof payload.message === 'string') {
      return { ok: Boolean(payload.ok), message: payload.message }
    }
  } catch {
    // 非 JSON 响应时退回通用提示
  }
  return { ok: false, message: fallback }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const result = await readResult(response, '升压站监视动作未生效，请稍后重试')
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    infoMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '升压站监视操作失败'
  }
}

async function requestPowerOn(row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/power-on`, { method: 'POST' })
    const result = await readResult(response, '站内送电申请未送达，请稍后重试')
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    infoMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '站内送电申请失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('升压站详情读取失败')
    }
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '升压站详情读取失败'
  }
}

function closeDetail() {
  detail.value = null
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) {
    query.set('keyword', keyword.value)
  }
  if (statusFilter.value) {
    query.set('status', statusFilter.value)
  }
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('升压站列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '运行中站点', value: rows.value.filter((row) => row['母线状态'] === '运行').length },
      { label: '停运站点', value: rows.value.filter((row) => row['母线状态'] === '停运').length },
      {
        label: '待调度确认',
        value: rows.value.filter((row) => String(row['母线状态']) !== '停运').length,
      },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '升压站监视列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.info-text { color: #1f6feb; }
.drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(16, 24, 40, 0.35);
  display: flex;
  justify-content: flex-end;
  z-index: 20;
}
.drawer {
  width: 380px;
  max-width: 90vw;
  background: #fff;
  height: 100%;
  padding: 16px 18px;
  overflow-y: auto;
}
.drawer-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.detail-list { margin: 12px 0; }
.detail-list div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
}
.detail-list dt { color: var(--muted); font-size: 13px; }
.detail-list dd { margin: 0; font-size: 13px; }
.detail-tip { color: var(--muted); font-size: 12px; }
</style>
