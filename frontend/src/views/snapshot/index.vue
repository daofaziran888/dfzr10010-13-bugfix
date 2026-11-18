<template>
  <section class="page" data-module="snapshot">
    <header class="page-head">
      <div>
        <h2>表快照管理</h2>
        <p class="page-desc">维护表快照的版本管理、保留策略与创建方式；快照模块先校验清理审计清单中清理结果清单的兼容性，再保留已有明细。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出表快照管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="checkCompatibility(row)">兼容性校验</button>
            <button class="link" type="button" @click="openCleanupDetail(row)">清理结果核对</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无表快照管理数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条表快照管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="panel">
      <div class="panel-head">
        <h3>
          快照清理核对
          <span class="panel-sub">与清理列表读取同一份清理结果清单，逐项比较删除行数、跳过分区和对应快照</span>
        </h3>
      </div>

      <div v-if="selectedSnapshot" class="snapshot-banner">
        <div>
          <strong>{{ selectedSnapshot['快照编号'] }}</strong>
          <span class="muted"> · 表 {{ selectedSnapshot['表名称'] }} · 版本 {{ selectedSnapshot['快照版本'] ?? '—' }}</span>
        </div>
        <div class="row-actions">
          <button class="btn" type="button" @click="checkCompatibility(selectedSnapshot)">重新校验兼容性</button>
          <button class="btn primary" type="button" @click="consumeLatestBatch">消费该表最新批次结果清单</button>
        </div>
      </div>

      <div v-if="compatReport" class="compat-box" :class="compatReport['兼容'] ? 'compat-ok' : 'compat-bad'">
        <strong>兼容性结论：{{ compatReport['兼容'] ? '兼容，可保留已有明细' : '不兼容，请保留明细并人工复核' }}</strong>
        <p class="muted">{{ compatReport['说明'] }}</p>
        <table class="data-table inner-table">
          <thead>
            <tr><th>批次编号</th><th>批次状态</th><th>结果明细数</th><th>明细完整</th><th>本快照保护分区</th><th>保护分区保留</th><th>兼容</th></tr>
          </thead>
          <tbody>
            <tr v-for="check in compatReport['校验批次']" :key="check['批次编号']">
              <td>{{ check['批次编号'] }}</td>
              <td>{{ check['批次状态'] }}</td>
              <td>{{ check['结果明细数'] }}</td>
              <td>{{ check['明细完整'] ? '是' : '否' }}</td>
              <td>{{ (check['本快照保护分区'] || []).join('、') || '—' }}</td>
              <td>{{ check['保护分区保留'] ? '是' : '否' }}</td>
              <td><span :class="['tag', check['兼容'] ? 'tag-green' : 'tag-red']">{{ check['兼容'] ? '兼容' : '不兼容' }}</span></td>
            </tr>
            <tr v-if="!compatReport['校验批次'].length">
              <td colspan="7" class="empty-state">该表暂无清理批次，兼容性校验为空态通过</td>
            </tr>
          </tbody>
        </table>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in cleanupColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in cleanupRows" :key="String(item.id)">
            <td>{{ item['批次编号'] }}</td>
            <td>{{ item['分区范围'] }}</td>
            <td><span :class="['tag', tagClass(item['结果状态'])]">{{ item['结果状态'] }}</span></td>
            <td>{{ item['删除行数'] }}</td>
            <td>{{ item['跳过原因'] ?? '—' }}</td>
            <td>{{ item['回滚记录编号'] ?? '—' }}</td>
            <td>{{ item['快照编号'] ?? '—' }}</td>
            <td class="note-cell">{{ item['说明'] }}</td>
          </tr>
          <tr v-if="!cleanupRows.length">
            <td :colspan="cleanupColumns.length" class="empty-state">请选择一条快照执行「清理结果核对」，数据与清理列表的清理结果清单同源</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>可恢复依据清单<span class="panel-sub">快照清单始终保留被跳过的恢复引用和分区范围，即使清理批次暂停也不丢失</span></h3>
        <button class="btn" type="button" @click="loadRecoverableRefs">刷新可恢复依据</button>
      </div>
      <table class="data-table">
        <thead>
          <tr><th>表名称</th><th>回滚记录编号</th><th>回滚状态</th><th>快照编号</th><th>快照版本</th><th>分区范围</th></tr>
        </thead>
        <tbody>
          <tr v-for="ref in recoverableRefs" :key="String(ref['回滚记录编号'])">
            <td>{{ ref['表名称'] }}</td>
            <td>{{ ref['回滚记录编号'] }}</td>
            <td>{{ ref['回滚状态'] }}</td>
            <td>{{ ref['快照编号'] }}</td>
            <td>{{ ref['快照版本'] ?? '—' }}</td>
            <td>{{ ref['分区范围'] }}</td>
          </tr>
          <tr v-if="!recoverableRefs.length">
            <td colspan="6" class="empty-state">暂无可恢复依据</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface ResultRow {
  id: number
  批次编号: string
  策略编号: string
  目标表名: string
  分区范围: string
  结果状态: string
  删除行数: number
  跳过原因: string | null
  回滚记录编号: string | null
  快照编号: string | null
  说明: string
}

interface RefRow {
  表名称: string
  回滚记录编号: string
  回滚状态: string
  快照编号: string
  快照版本: string | null
  分区范围: string
}

interface CompatCheck {
  批次编号: string
  批次状态: string
  结果明细数: number
  明细完整: boolean
  本快照保护分区: string[]
  保护分区保留: boolean
  兼容: boolean
}

interface CompatReport {
  快照编号: string
  表名称: string
  快照版本: string | null
  兼容: boolean
  校验批次: CompatCheck[]
  说明: string
}

const ENDPOINT = '/api/snapshot'
const columns = ['快照编号', '表名称', '快照时间', '快照版本', '快照大小', '保留天数', '创建方式', '快照状态']
const cleanupColumns = ['批次编号', '分区范围', '结果状态', '删除行数', '跳过原因', '回滚记录编号', '快照编号', '说明']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const selectedSnapshot = ref<Row | null>(null)
const cleanupRows = ref<ResultRow[]>([])
const compatReport = ref<CompatReport | null>(null)
const recoverableRefs = ref<RefRow[]>([])

const stats = computed(() => [
  { label: '快照记录总数', value: rows.value.length },
  { label: '保留中快照', value: rows.value.filter((r) => r.status === '保留中').length },
  { label: '可恢复依据', value: recoverableRefs.value.length },
])

function tagClass(status: string): string {
  if (status === '已删除' || status === '已完成') return 'tag-green'
  if (status === '已跳过' || status === '已暂停') return 'tag-amber'
  if (status === '失败' || status === '中断') return 'tag-red'
  return 'tag-blue'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function checkCompatibility(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/cleanup/compatibility`)
    if (!response.ok) {
      throw new Error('清理结果清单兼容性校验失败')
    }
    compatReport.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '兼容性校验读取失败'
  }
}

async function openCleanupDetail(row: Row) {
  selectedSnapshot.value = row
  errorMessage.value = ''
  try {
    const table = String(row['表名称'] ?? '')
    const response = await request(`/api/cleanup/results?table=${encodeURIComponent(table)}`)
    if (!response.ok) {
      throw new Error('清理结果清单读取失败')
    }
    const payload = await response.json()
    cleanupRows.value = payload.items ?? []
    await checkCompatibility(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清理结果清单读取失败'
  }
}

async function consumeLatestBatch() {
  if (!selectedSnapshot.value) {
    return
  }
  errorMessage.value = ''
  const latest = cleanupRows.value.reduce<string | null>((acc, item) => {
    const batch = String(item['批次编号'] ?? '')
    if (!acc || batch > acc) return batch
    return acc
  }, null)
  if (!latest) {
    errorMessage.value = '该表暂无清理结果清单可消费'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/${selectedSnapshot.value.id}/cleanup/consume`, {
      method: 'POST',
      body: JSON.stringify({ batch: latest }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '消费清理结果清单失败')
    }
    errorMessage.value = payload.message
    await checkCompatibility(selectedSnapshot.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '消费清理结果清单失败'
  }
}

async function loadRecoverableRefs() {
  try {
    const response = await request(`${ENDPOINT}/cleanup/recoverable-refs`)
    if (!response.ok) {
      throw new Error('可恢复依据清单读取失败')
    }
    const payload = await response.json()
    recoverableRefs.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '可恢复依据清单读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('快照记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表快照管理列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadRecoverableRefs()
})
</script>
