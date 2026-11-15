<template>
    <div class="route-container">
        <HeaderBar />
        <main>
            <Navigation />
            <div class="reservation-conditions-container-wrapper">
                <SPHeaderBar />
                <div class="reservation-conditions-container">
                    <Breadcrumbs :crumbs="[
                        { name: 'ホーム', path: '/' },
                        { name: '録画予約', path: '/reservations/' },
                        { name: 'キーワード自動予約', path: '/reservations/conditions', disabled: true },
                    ]" />
                    <div class="reservation-conditions">
                        <div class="reservation-conditions__header">
                            <h2 class="reservation-conditions__title">
                                キーワード自動予約
                                <span class="reservation-conditions__count">{{ conditions.length }}件</span>
                            </h2>
                            <v-btn class="reservation-conditions__add" color="primary" variant="flat" @click="openNewEditor">
                                <Icon icon="fluent:add-20-filled" width="20px" height="20px" class="mr-1" />
                                条件を追加
                            </v-btn>
                        </div>
                        <div class="reservation-conditions__description">
                            条件に合う番組が番組表に出てくると、自動で録画予約されます。番組検索の画面の「この条件で自動予約」からも追加できます。
                        </div>
                        <div v-if="isLoading && conditions.length === 0" class="reservation-conditions__empty">
                            <v-progress-circular indeterminate color="secondary" />
                        </div>
                        <div v-else-if="conditions.length === 0" class="reservation-conditions__empty">
                            <Icon icon="fluent:calendar-search-20-regular" width="54px" height="54px" />
                            <div class="reservation-conditions__empty-message">キーワード自動予約の条件はまだありません。</div>
                        </div>
                        <div v-else class="reservation-conditions__list">
                            <div v-for="condition in conditions" :key="condition.id" class="reservation-condition"
                                :class="{ 'reservation-condition--disabled': !condition.program_search_condition.is_enabled }">
                                <div class="reservation-condition__main" v-ripple @click="openEditor(condition)">
                                    <div class="reservation-condition__keyword">
                                        {{ condition.program_search_condition.keyword || '(キーワードなし)' }}
                                    </div>
                                    <div class="reservation-condition__summary">
                                        <span v-for="label in getSummaryLabels(condition)" :key="label" class="reservation-condition__chip">{{ label }}</span>
                                    </div>
                                    <div class="reservation-condition__reserves">
                                        <Icon icon="fluent:timer-16-regular" width="16px" height="16px" />
                                        予約 {{ condition.reservation_count }} 件
                                    </div>
                                </div>
                                <div class="reservation-condition__actions">
                                    <v-switch
                                        :model-value="condition.program_search_condition.is_enabled"
                                        color="primary" density="compact" hide-details
                                        :disabled="updatingIds.has(condition.id)"
                                        @update:model-value="(value) => toggleEnabled(condition, value === true)" />
                                    <v-btn icon variant="text" size="small" @click="openEditor(condition)">
                                        <Icon icon="fluent:edit-20-regular" width="20px" height="20px" />
                                    </v-btn>
                                    <v-btn icon variant="text" size="small" :disabled="updatingIds.has(condition.id)" @click="deleteCondition(condition)">
                                        <Icon icon="fluent:delete-20-regular" width="20px" height="20px" />
                                    </v-btn>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </main>
        <v-dialog v-model="isEditorOpen" max-width="440" scrollable>
            <v-card class="reservation-condition-editor">
                <v-card-title class="reservation-condition-editor__title">
                    {{ editingConditionId === null ? 'キーワード自動予約を追加' : 'キーワード自動予約を編集' }}
                </v-card-title>
                <v-card-text class="reservation-condition-editor__body">
                    <ProgramSearchFilters
                        v-if="editingCondition !== null"
                        v-model:condition="editingCondition"
                        :channels="allChannels"
                        :is-searching="isSaving"
                        is-condition-mode
                        submit-label="保存"
                        @search="saveEditingCondition" />
                </v-card-text>
            </v-card>
        </v-dialog>
    </div>
</template>
<script lang="ts" setup>

import { computed, onMounted, ref, toRaw } from 'vue';

import Breadcrumbs from '@/components/Breadcrumbs.vue';
import HeaderBar from '@/components/HeaderBar.vue';
import Navigation from '@/components/Navigation.vue';
import ProgramSearchFilters from '@/components/Programs/ProgramSearchFilters.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import Message from '@/message';
import { ILiveChannel } from '@/services/Channels';
import { IProgramSearchCondition } from '@/services/Programs';
import ReservationConditions, { IReservationCondition } from '@/services/ReservationConditions';
import Reservations, { IRecordSettingsDefault } from '@/services/Reservations';
import useChannelsStore from '@/stores/ChannelsStore';
import useUserStore from '@/stores/UserStore';

const channelsStore = useChannelsStore();

const conditions = ref<IReservationCondition[]>([]);
const isLoading = ref(true);
const updatingIds = ref<Set<number>>(new Set());

// 編集中の条件 (null のときは閉じている)
const isEditorOpen = ref(false);
const editingConditionId = ref<number | null>(null);
const editingCondition = ref<IProgramSearchCondition | null>(null);
const isSaving = ref(false);

const allChannels = computed<ILiveChannel[]>(() => Object.values(channelsStore.channels_list).flat());

const DAYS_OF_WEEK = ['日', '月', '火', '水', '木', '金', '土'];

const createEmptyCondition = (): IProgramSearchCondition => ({
    is_enabled: true,
    keyword: '',
    exclude_keyword: '',
    note: '',
    is_title_only: false,
    is_case_sensitive: false,
    is_fuzzy_search_enabled: false,
    is_regex_search_enabled: false,
    service_ranges: null,
    genre_ranges: null,
    is_exclude_genre_ranges: false,
    date_ranges: null,
    is_exclude_date_ranges: false,
    duration_range_min: null,
    duration_range_max: null,
    broadcast_type: 'All',
    duplicate_title_check_scope: 'None',
    duplicate_title_check_period_days: 6,
});

// 一覧のカードに出す条件の要約
const getSummaryLabels = (condition: IReservationCondition): string[] => {
    const c = condition.program_search_condition;
    const labels: string[] = [];
    if (c.is_title_only) labels.push('番組名のみ');
    if (c.exclude_keyword) labels.push(`除外: ${c.exclude_keyword}`);
    labels.push(c.service_ranges === null ? 'すべてのチャンネル' : `${c.service_ranges.length} チャンネル`);
    if (c.genre_ranges !== null) {
        const genres = c.genre_ranges.map((genre) => genre.middle === 'すべて' ? genre.major : genre.middle);
        labels.push((c.is_exclude_genre_ranges ? 'ジャンル除外: ' : '') + genres.slice(0, 3).join('・') + (genres.length > 3 ? ` ほか${genres.length - 3}` : ''));
    }
    if (c.date_ranges !== null) {
        const days = [...new Set(c.date_ranges.map((date) => DAYS_OF_WEEK[date.start_day_of_week]))].join('');
        const first = c.date_ranges[0];
        labels.push(`${c.is_exclude_date_ranges ? '日時除外: ' : ''}${days} ${first.start_hour}時〜`);
    }
    if (c.duration_range_min !== null || c.duration_range_max !== null) {
        labels.push(`${c.duration_range_min ?? ''}〜${c.duration_range_max ?? ''}分`);
    }
    if (c.broadcast_type === 'FreeOnly') labels.push('無料のみ');
    if (c.duplicate_title_check_scope !== 'None') labels.push(`重複は${c.duplicate_title_check_period_days}日避ける`);
    return labels;
};

const fetchConditions = async () => {
    const result = await ReservationConditions.fetchReservationConditions();
    if (result !== null) {
        conditions.value = result.reservation_conditions;
    }
    isLoading.value = false;
};

const toggleEnabled = async (condition: IReservationCondition, isEnabled: boolean) => {
    updatingIds.value.add(condition.id);
    const programSearchCondition = { ...structuredClone(toRaw(condition.program_search_condition)), is_enabled: isEnabled };
    const recordSettings = { ...structuredClone(toRaw(condition.record_settings)), is_enabled: isEnabled };
    const result = await ReservationConditions.updateReservationCondition(condition.id, programSearchCondition, recordSettings);
    if (result !== null) {
        conditions.value = conditions.value.map((item) => item.id === condition.id ? result : item);
        Message.success(isEnabled ? '自動予約を有効にしました。' : '自動予約を無効にしました。');
    }
    updatingIds.value.delete(condition.id);
};

const deleteCondition = async (condition: IReservationCondition) => {
    const name = condition.program_search_condition.keyword || '(キーワードなし)';
    if (window.confirm(`「${name}」の自動予約を削除しますか？この条件で入った予約も消えます。`) === false) {
        return;
    }
    updatingIds.value.add(condition.id);
    const isSuccess = await ReservationConditions.deleteReservationCondition(condition.id);
    if (isSuccess === true) {
        conditions.value = conditions.value.filter((item) => item.id !== condition.id);
        Message.success('自動予約を削除しました。');
    }
    updatingIds.value.delete(condition.id);
};

const openEditor = (condition: IReservationCondition) => {
    editingConditionId.value = condition.id;
    editingCondition.value = structuredClone(toRaw(condition.program_search_condition));
    isEditorOpen.value = true;
};

const openNewEditor = () => {
    editingConditionId.value = null;
    editingCondition.value = createEmptyCondition();
    isEditorOpen.value = true;
};

const saveEditingCondition = async () => {
    if (editingCondition.value === null) return;
    const condition = structuredClone(toRaw(editingCondition.value));
    if (condition.keyword.trim() === '' && condition.genre_ranges === null && condition.date_ranges === null) {
        Message.warning('キーワードかジャンル・日時のどれかを指定してください。');
        return;
    }
    isSaving.value = true;
    if (editingConditionId.value === null) {
        const recordSettings = await Reservations.fetchDefaultRecordSettings().catch(() => structuredClone(IRecordSettingsDefault));
        const isSuccess = await ReservationConditions.addReservationCondition(condition, recordSettings);
        if (isSuccess === true) {
            Message.success('自動予約を追加しました。');
            isEditorOpen.value = false;
            await fetchConditions();
        }
    } else {
        const current = conditions.value.find((item) => item.id === editingConditionId.value);
        const recordSettings = current !== undefined ? structuredClone(toRaw(current.record_settings)) : structuredClone(IRecordSettingsDefault);
        const result = await ReservationConditions.updateReservationCondition(editingConditionId.value, condition, recordSettings);
        if (result !== null) {
            conditions.value = conditions.value.map((item) => item.id === result.id ? result : item);
            Message.success('自動予約を保存しました。');
            isEditorOpen.value = false;
        }
    }
    isSaving.value = false;
};

onMounted(async () => {
    await Promise.all([
        useUserStore().fetchUser(),
        channelsStore.update(),
        fetchConditions(),
    ]);
});

</script>
<style lang="scss" scoped>

.reservation-conditions-container-wrapper {
    display: flex;
    flex-direction: column;
    width: 100%;
    min-width: 0;
}

.reservation-conditions-container {
    display: flex;
    flex-direction: column;
    width: 100%;
    max-width: 1000px;
    padding: 20px;
    margin: 0 auto;
    @include smartphone-vertical {
        padding: 16px 8px !important;
        padding-top: 8px !important;
    }
}

.reservation-conditions {
    margin-top: 12px;

    &__header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
    }

    &__title {
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 24px;
        font-weight: 700;
        @include smartphone-vertical {
            font-size: 20px;
        }
    }

    &__count {
        font-size: 14px;
        font-weight: 400;
        color: rgb(var(--v-theme-text-darken-1));
    }

    &__add {
        letter-spacing: 0;
    }

    &__description {
        margin-top: 8px;
        font-size: 13.5px;
        color: rgb(var(--v-theme-text-darken-1));
    }

    &__empty {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 12px;
        padding: 60px 0;
        color: rgb(var(--v-theme-text-darken-1));
    }

    &__list {
        display: flex;
        flex-direction: column;
        gap: 10px;
        margin-top: 16px;
    }
}

.reservation-condition {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 8px 4px 0;
    border-radius: 12px;
    background: rgb(var(--v-theme-background-lighten-1));
    transition: opacity 0.15s;

    &--disabled {
        opacity: 0.55;
    }

    &__main {
        flex: 1;
        min-width: 0;
        padding: 12px 16px;
        border-radius: 12px;
        cursor: pointer;
    }

    &__keyword {
        font-size: 16px;
        font-weight: 700;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    &__summary {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 6px;
    }

    &__chip {
        padding: 2px 8px;
        border-radius: 99px;
        font-size: 12px;
        background: rgb(var(--v-theme-background-lighten-2));
        color: rgb(var(--v-theme-text-darken-1));
    }

    &__reserves {
        display: flex;
        align-items: center;
        gap: 4px;
        margin-top: 8px;
        font-size: 13px;
        color: rgb(var(--v-theme-text-darken-1));
    }

    &__actions {
        display: flex;
        align-items: center;
        gap: 2px;
        flex-shrink: 0;
    }
}

.reservation-condition-editor {
    &__title {
        font-size: 18px;
        font-weight: 700;
    }

    &__body {
        padding: 0 12px 12px !important;
    }
}

</style>
