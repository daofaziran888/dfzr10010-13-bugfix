<template>
  <section class="page" data-module="cleanup">
    <header class="page-head">
      <div>
        <h2>数据过期清理管理</h2>
        <p class="page-desc">维护数据过期的条件判断、清理方式与执行周期；清理结果清单按批次记录删除行数、跳过分区与对应快照，恢复链保护分区不可删除。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出数据过期清理清单</button>
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
            <button class="link" type="button" @click="runAction('启用清理', row)">启用清理</button>
            <button class="link" type="button" @click="runAction('立即执行', row)">立即执行</button>
            <button class="link" type="button" @click="runAction('继续清理', row)">继续清理</button>
            <button class="link danger" type="button" @click="runAction('关闭清理', row)">关闭清理</button>
            <button class="link" type="button" @click="openResults(row)">清理结果清单</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据过期清理数据，可先登记清理策略</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据过期清理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="panel">
      <div class="panel-head">
        <h3>清理结果清单<span class="panel-sub">同一批次的实际删除行数、被恢复链保护跳过的分区与对应快照；两个入口共用同一份数据</span></h3>
        <div class="panel-tools">
          <label class="filter-item">
            <span>目标表名</span>
            <input v-model="resultFilter.table" placeholder="如 orders" @keyup.enter="loadResults" />
          </label>
          <label class="filter-item">
            <span>批次编号</span>
            <input v-model="resultFilter.batch" placeholder="如 BATCH-2026-11-18-01" @keyup.enter="loadResults" />
          </label>
          <button class="btn" type="button" @click="loadResults">刷新清单</button>
        </div>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in resultColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in resultRows" :key="String(item.id)">
            <td>{{ item['批次编号'] }}</td>
            <td>{{ item['策略编号'] }}</td>
            <td>{{ item['目标表名'] }}</td>
            <td>{{ item['分区范围'] }}</td>
            <td><span :class="['tag', tagClass(item['结果状态'])]">{{ item['结果状态'] }}</span></td>
            <td>{{ item['删除行数'] }}</td>
            <td>{{ item['跳过原因'] ?? '—' }}</td>
            <td>{{ item['回滚记录编号'] ?? '—' }}</td>
            <td>{{ item['快照编号'] ?? '—' }}</td>
            <td class="note-cell">{{ item['说明'] }}</td>
          </tr>
          <tr v-if="!resultRows.length">
            <td :colspan="resultColumns.length" class="empty-state">暂无清理结果，执行清理后在此核对删除行数与跳过分区</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ resultRows.length }} 条结果明细，本批次实际删除行数合计：<strong>{{ resultDeletedTotal }}</strong></span>
      </footer>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>清理审计清单<span class="panel-sub">恢复结论、被跳过的恢复引用与分区范围，以及其它业务模块消费后同步的版本依据</span></h3>
        <button class="btn" type="button" @click="loadAudits">刷新审计</button>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in auditColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in auditRows" :key="String(item.id)">
            <td>{{ item['批次编号'] }}</td>
            <td>{{ item['策略编号'] }}</td>
            <td>{{ item['目标表名'] }}</td>
            <td><span :class="['tag', tagClass(item['批次状态'])]">{{ item['批次状态'] }}</span></td>
            <td>{{ item['清理行数'] }}</td>
            <td>{{ (item['跳过分区'] || []).join('、') || '—' }}</td>
            <td>
              <div v-for="ref in item['恢复引用']" :key="ref['分区值']" class="ref-line">
                {{ ref['分区值'] }} ← {{ ref['回滚单'] }} / {{ ref['快照编号'] }}
              </div>
              <span v-if="!(item['恢复引用'] || []).length">—</span>
            </td>
            <td>{{ (item['失败分区'] || []).join('、') || '—' }}</td>
            <td>{{ (item['空态分区'] || []).join('、') || '—' }}</td>
            <td>
              <template v-if="item['版本依据']">
                <div>{{ item['版本依据']['快照编号'] }} / {{ item['版本依据']['快照版本'] }}</div>
                <div class="muted">来源批次 {{ item['版本依据']['消费批次'] }}</div>
              </template>
              <span v-else>待快照模块同步</span>
            </td>
            <td class="note-cell">{{ item['恢复结论'] }}</td>
          </tr>
          <tr v-if="!auditRows.length">
            <td :colspan="auditColumns.length" class="empty-state">暂无审计记录</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

interface ResultRow {
  id: number
  批次编号: string
  策略编号: string
  目标表名: string
  分区字段: string
  分区值: string
  分区范围: string
  结果状态: string
  删除行数: number
  跳过原因: string | null
  回滚记录编号: string | null
  快照编号: string | null
  说明: string
  执行日期: string
}

interface RecoverRef {
  分区值: string
  回滚单: string
  快照编号: string
  快照版本: string | null
}

interface VersionBasis {
  快照编号: string
  快照版本: string
  快照时间: string
  消费批次: string
}

interface AuditRow {
  id: number
  批次编号: string
  策略编号: string
  目标表名: string
  过期条件: string
  批次状态: string
  清理行数: number
  跳过分区: string[]
  恢复引用: RecoverRef[]
  失败分区: string[]
  空态分区: string[]
  版本冲突: boolean
  恢复结论: string
  版本依据: VersionBasis | null
  执行日期: string
}

const ENDPOINT = '/api/cleanup'
const columns = ['策略编号', '目标表名', '过期条件', '清理方式', '执行周期', '最后清理', '清理行数', '策略状态']
const resultColumns = ['批次编号', '策略编号', '目标表名', '分区范围', '结果状态', '删除行数', '跳过原因', '回滚记录编号', '快照编号', '说明']
const auditColumns = ['批次编号', '策略编号', '目标表名', '批次状态', '清理行数', '跳过分区', '恢复引用', '失败分区', '空态分区', '版本依据', '恢复结论']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const resultRows = ref<ResultRow[]>([])
const resultDeletedTotal = ref(0)
const resultFilter = ref<{ table: string; batch: string }>({ table: '', batch: '' })
const auditRows = ref<AuditRow[]>([])

const stats = computed(() => [
  { label: '清理策略总数', value: rows.value.length },
  { label: '已启用清理', value: rows.value.filter((r) => r.status === '已启用').length },
  { label: '已关闭清理', value: rows.value.filter((r) => r.status === '已关闭').length },
  { label: '清理结果明细', value: resultRows.value.length },
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

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '数据过期清理动作未生效，请稍后重试')
    }
    errorMessage.value = payload.message
    await reload()
    await loadResults()
    await loadAudits()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据过期清理操作失败'
  }
}

function openResults(row: Row) {
  resultFilter.value.table = String(row['目标表名'] ?? '')
  resultFilter.value.batch = ''
  void loadResults()
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('清理策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据过期清理列表读取失败'
  }
}

async function loadResults() {
  const params = new URLSearchParams()
  if (resultFilter.value.table) params.set('table', resultFilter.value.table)
  if (resultFilter.value.batch) params.set('batch', resultFilter.value.batch)
  try {
    const response = await request(`${ENDPOINT}/results?${params.toString()}`)
    if (!response.ok) {
      throw new Error('清理结果清单读取失败')
    }
    const payload = await response.json()
    resultRows.value = payload.items ?? []
    resultDeletedTotal.value = payload['删除行数合计'] ?? 0
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清理结果清单读取失败'
  }
}

async function loadAudits() {
  try {
    const response = await request(`${ENDPOINT}/audits`)
    if (!response.ok) {
      throw new Error('清理审计清单读取失败')
    }
    const payload = await response.json()
    auditRows.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清理审计清单读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadResults()
  void loadAudits()
})
</script>
