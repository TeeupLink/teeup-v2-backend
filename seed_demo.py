"""App Store 심사·스크린샷용 데모 데이터 시드.

지정한 사용자를 클럽장으로 하는 클럽 하나를 만들고, 멤버·공지·회비·라운딩/소셜 모임·
스코어 기록을 채운다. 여러 번 실행해도 안전하다(클럽명 기준으로 이미 있으면 건너뜀).

사용:
    python seed_demo.py                                  # ejto100@gmail.com 이 클럽장
    python seed_demo.py --leader-email someone@x.com
    python seed_demo.py --reviewer-email reviewer@teeup.run   # 리뷰어 계정도 멤버로 가입

DATABASE_URL 은 .env(config.Settings) 를 그대로 사용한다.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import random
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from database import SessionLocal
from models import (
    User, Club, ClubMembership, ClubNotice, ClubFee, ClubRegion, Sido, Gungu,
    Meeting, MeetingParticipant, UserScoreHistory, MeetingResult, Score,
    Provider, Gender, UserStatus, ClubType, ClubStatus, ClubRole, MembershipStatus,
    MeetingType, MeetingSubtype, SocialType, SettlementMethod, ParticipantType,
    ParticipantStatus, BillingCycle, HandicapUpdateMethod,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed_demo")

CLUB_NAME = "티업 골프 동호회"
MOCK_EMAIL_DOMAIN = "demo.teeup.run"
MOCK_PASSWORD = hashlib.sha256("demo1234!".encode()).hexdigest()

# (실명, 닉네임, 성별, 평균타수, 핸디캡, 출생연도)
MOCK_MEMBERS = [
    ("박준혁", "버디헌터", Gender.MALE, 84, 12.0, 1988),
    ("이서연", "페어웨이요정", Gender.FEMALE, 92, 18.5, 1992),
    ("김태호", "롱드라이버", Gender.MALE, 79, 8.0, 1985),
    ("정민지", "그린위의여왕", Gender.FEMALE, 96, 22.0, 1995),
    ("최동욱", "이글메이커", Gender.MALE, 88, 14.5, 1983),
    ("한지우", "아이언마스터", Gender.FEMALE, 90, 16.0, 1990),
    ("오승현", "퍼팅장인", Gender.MALE, 82, 10.5, 1987),
    ("윤소라", "홀인원소망", Gender.FEMALE, 101, 26.0, 1997),
    ("장현우", "벙커탈출왕", Gender.MALE, 94, 20.0, 1981),
    ("배수진", "티샷여신", Gender.FEMALE, 87, 13.5, 1993),
    ("송재민", "파세이브", Gender.MALE, 91, 17.0, 1989),
    ("임하늘", "보기플레이어", Gender.FEMALE, 98, 24.0, 1996),
]

GOLF_COURSES = [
    ("레이크사이드 CC", "경기 용인시 처인구"),
    ("스카이72 오션코스", "인천 중구 영종도"),
    ("남서울 CC", "경기 성남시 분당구"),
    ("베어크리크 GC", "경기 포천시"),
    ("파인비치 GL", "전남 해남군"),
]

# 18홀 파 배치 (전반 36 + 후반 36 = 파72, par3 x4 / par4 x10 / par5 x4)
HOLE_PARS = [4, 4, 3, 5, 4, 4, 3, 4, 5, 4, 3, 5, 4, 4, 3, 4, 5, 4]



def get_or_create_user(db: Session, email: str, **fields) -> tuple[User, bool]:
    user = db.query(User).filter(User.email == email).first()
    if user:
        return user, False
    user = User(email=email, **fields)
    db.add(user)
    db.flush()
    return user, True


def ensure_membership(db: Session, club: Club, user: User, role: ClubRole) -> bool:
    m = (db.query(ClubMembership)
         .filter(ClubMembership.club_id == club.id, ClubMembership.user_id == user.id)
         .first())
    if m:
        if m.role != role or m.status != MembershipStatus.ACTIVE:
            m.role, m.status = role, MembershipStatus.ACTIVE
        return False
    db.add(ClubMembership(club_id=club.id, user_id=user.id, role=role, status=MembershipStatus.ACTIVE))
    return True


def pick_seoul(db: Session) -> tuple[str, list[str]]:
    sido = db.query(Sido).filter(Sido.name.like("서울%")).first() or db.query(Sido).first()
    if not sido:
        raise RuntimeError("sido 테이블이 비어 있습니다. 지역 데이터를 먼저 채우세요.")
    gungus = (db.query(Gungu)
              .filter(Gungu.code.like(f"{sido.code}%"), Gungu.is_active.is_(True))
              .order_by(Gungu.code).limit(3).all())
    return sido.code, [g.code for g in gungus]


def add_round(db: Session, club: Club, leader: User, participants: list[User], *,
              name: str, description: str, course: tuple[str, str], when: datetime,
              status: str, max_participants: int = 16) -> Meeting:
    existing = db.query(Meeting).filter(Meeting.club_id == club.id, Meeting.name == name).first()
    if existing:
        return existing
    course_name, location = course
    tee_count = max(1, (len(participants) + 3) // 4)
    m = Meeting(
        name=name,
        description=description,
        location=location,
        meeting_time=when,
        tee_times=[f"{7 + i:02d}:{'00' if i % 2 == 0 else '08'}" for i in range(tee_count)],
        max_participants=max_participants,
        meeting_type=MeetingType.ROUND,
        meeting_subtype=MeetingSubtype.REGULAR,
        total_cost=195000,
        green_fee=150000,
        caddy_fee=35000,
        cart_fee=10000,
        settlement_method=SettlementMethod.EQUAL_SPLIT,
        course_name=course_name,
        hole_count=18,
        reservation_name=leader.realname,
        club_id=club.id,
        status=status,
        application_deadline=when - timedelta(days=2),
        created_by=leader.id,
    )
    db.add(m)
    db.flush()
    for idx, u in enumerate(participants):
        db.add(MeetingParticipant(
            meeting_id=m.id, user_id=u.id,
            participant_type=ParticipantType.USER,
            status=ParticipantStatus.CONFIRMED,
            handicap=float(u.handicap or 18),
            average_score=u.average_score or 90,
            is_newbie=(idx % 6 == 5),
        ))
    return m


def add_social(db: Session, club: Club, leader: User, participants: list[User], *,
               name: str, description: str, venue: str, location: str, when: datetime,
               status: str, social_type: SocialType, cost: int) -> Meeting:
    existing = db.query(Meeting).filter(Meeting.club_id == club.id, Meeting.name == name).first()
    if existing:
        return existing
    m = Meeting(
        name=name, description=description, location=location,
        meeting_time=when, max_participants=20,
        meeting_type=MeetingType.SOCIAL, social_type=social_type,
        venue_name=venue, social_cost=cost,
        settlement_method=SettlementMethod.EQUAL_SPLIT,
        club_id=club.id, status=status,
        application_deadline=when - timedelta(days=1),
        created_by=leader.id,
    )
    db.add(m)
    db.flush()
    for u in participants:
        db.add(MeetingParticipant(meeting_id=m.id, user_id=u.id,
                                  participant_type=ParticipantType.USER,
                                  status=ParticipantStatus.CONFIRMED))
    return m


def make_hole_strokes(gross: int, rng: random.Random) -> list[int]:
    """합계가 정확히 gross 인 18홀 타수 생성.

    총타(UserScoreHistory.gross_score)와 홀별 합이 어긋나면 화면에서
    값이 서로 다르게 보이므로 반드시 일치시킨다.
    """
    avg_over = (gross - sum(HOLE_PARS)) / len(HOLE_PARS)
    strokes = []
    for par in HOLE_PARS:
        delta = max(-1, min(4, round(rng.gauss(avg_over, 0.9))))  # 홀당 버디~트리플보기+
        strokes.append(max(2, par + delta))
    guard = 0
    while sum(strokes) != gross and guard < 10000:
        guard += 1
        i = rng.randrange(len(HOLE_PARS))
        par = HOLE_PARS[i]
        if sum(strokes) < gross and strokes[i] < par + 4:
            strokes[i] += 1
        elif sum(strokes) > gross and strokes[i] > max(2, par - 1):
            strokes[i] -= 1
    return strokes


def record_scores(db: Session, meeting: Meeting, participants: list[User], rng: random.Random) -> int:
    """완료된 라운딩에 참가자별 스코어 기록. 이미 있으면 건너뜀."""
    if db.query(UserScoreHistory).filter(UserScoreHistory.meeting_id == meeting.id).count():
        return 0
    # 홀별 스코어를 붙이려면 참가자 레코드가 필요하다 (scores.participant_id)
    part_by_user = {
        p.user_id: p
        for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting.id)
        if p.user_id is not None
    }
    results = []
    for u in participants:
        base = u.average_score or 90
        gross = max(68, min(120, base + rng.randint(-6, 6)))
        hc = Decimal(str(u.handicap or 18))
        net = Decimal(gross) - hc
        db.add(UserScoreHistory(user_id=u.id, meeting_id=meeting.id,
                                gross_score=gross, net_score=net, handicap_used=hc,
                                handicap_after_round=hc, played_at=meeting.meeting_time))

        # 홀별 스코어 (합계는 위 gross 와 정확히 일치).
        # 이미 있으면 건너뛴다 — 안 그러면 재실행 시 18홀이 덧쌓인다.
        part = part_by_user.get(u.id)
        if part is not None and not db.query(Score).filter(Score.participant_id == part.id).count():
            for i, strokes in enumerate(make_hole_strokes(gross, rng), start=1):
                par = HOLE_PARS[i - 1]
                db.add(Score(participant_id=part.id, hole_number=i,
                             strokes=strokes, par=par, score_to_par=strokes - par))
            part.has_hole_scores = True

        results.append((u.id, net))
    results.sort(key=lambda r: r[1])
    for rank, (uid, _) in enumerate(results, 1):
        db.add(MeetingResult(meeting_id=meeting.id, user_id=uid, rank=rank,
                             completed_at=meeting.meeting_time + timedelta(hours=5)))
    return len(results)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--leader-email", default="ejto100@gmail.com")
    ap.add_argument("--reviewer-email", default="reviewer@teeup.run")
    ap.add_argument("--seed", type=int, default=20260914)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    db = SessionLocal()
    try:
        leader = db.query(User).filter(User.email == args.leader_email).first()
        if not leader:
            raise SystemExit(f"클럽장 계정을 찾을 수 없습니다: {args.leader_email} (먼저 앱에서 로그인해 계정을 만드세요)")
        log.info("클럽장: %s (#%s, %s)", leader.realname or leader.nickname, leader.id, leader.email)

        # ── 멤버 ───────────────────────────────────────────────────────────
        members: list[User] = []
        created = 0
        for i, (realname, nick, gender, avg, hc, year) in enumerate(MOCK_MEMBERS, 1):
            u, is_new = get_or_create_user(
                db, f"demo{i:02d}@{MOCK_EMAIL_DOMAIN}",
                nickname=nick, realname=realname, password=MOCK_PASSWORD,
                phone_number=f"010-9{i:03d}-{1000 + i * 37:04d}",
                gender=gender, birthdate=datetime(year, rng.randint(1, 12), rng.randint(1, 28)),
                average_score_init=avg, average_score=avg,
                handicap_init=Decimal(str(hc)), handicap=Decimal(str(hc)),
                handicap_update_method=HandicapUpdateMethod.AUTO if hasattr(HandicapUpdateMethod, "AUTO") else None,
                provider=Provider.LOCAL, status=UserStatus.ACTIVE,
                # privacy_collection 이 빠지면 routers/auth.py 의 get_current_active_user 가
                # 세 플래그(terms_agreement·privacy_policy·privacy_collection)를 모두 보기 때문에
                # 403 TERMS_NOT_AGREED 로 막혀 데모 계정이 약관 동의 화면에서 못 빠져나온다.
                needs_terms_agreement=False, terms_agreement=True, privacy_policy=True,
                privacy_collection=True,
            )
            members.append(u)
            created += int(is_new)
        log.info("목 멤버 %d명 (신규 %d)", len(members), created)

        reviewer = db.query(User).filter(User.email == args.reviewer_email).first()
        if reviewer:
            log.info("리뷰어 계정 포함: %s (#%s)", reviewer.email, reviewer.id)

        # ── 클럽 ───────────────────────────────────────────────────────────
        club = db.query(Club).filter(Club.name == CLUB_NAME, Club.deleted_at.is_(None)).first()
        sido_code, gungu_codes = pick_seoul(db)
        if not club:
            club = Club(
                name=CLUB_NAME,
                display_id="teeup-demo",
                sido_code=sido_code,
                type=ClubType.REGULAR,
                description=(
                    "매월 둘째·넷째 주 토요일에 정기 라운딩을 진행하는 서울·경기권 골프 동호회입니다.\n"
                    "실력보다 매너를 중시하며, 초보자도 부담 없이 함께할 수 있습니다.\n"
                    "라운딩 후에는 가벼운 식사 모임으로 친목을 다집니다."
                ),
                member_count=30,
                representative_name=leader.realname or leader.nickname,
                location="서울 강남·송파 / 경기 용인·성남",
                contact_info="pixencrew@gmail.com",
                additional_info="회비는 분기별 정산, 게스트 동반 가능 (사전 공지 필수)",
                status=ClubStatus.ACTIVE,
                settlement_enabled=True,
            )
            db.add(club)
            db.flush()
            for code in gungu_codes:
                db.add(ClubRegion(club_id=club.id, gungu_code=code))
            log.info("클럽 생성: %s (#%s)", club.name, club.id)
        else:
            log.info("클럽 이미 존재: %s (#%s) — 멤버/모임만 보강", club.name, club.id)

        ensure_membership(db, club, leader, ClubRole.LEADER)
        ensure_membership(db, club, members[0], ClubRole.MANAGER)
        for u in members[1:]:
            ensure_membership(db, club, u, ClubRole.MEMBER)
        if reviewer:
            ensure_membership(db, club, reviewer, ClubRole.MEMBER)
        db.flush()

        # ── 회비 ───────────────────────────────────────────────────────────
        if not db.query(ClubFee).filter(ClubFee.club_id == club.id).count():
            db.add(ClubFee(club_id=club.id, name="정기 회비", amount=Decimal("30000"),
                           cycle=BillingCycle.QUARTERLY, description="분기별 운영비 (경품·식사 보조)",
                           is_active=True, created_by=leader.id))

        # ── 공지 ───────────────────────────────────────────────────────────
        if not db.query(ClubNotice).filter(ClubNotice.club_id == club.id).count():
            notices = [
                ("10월 정기 라운딩 안내", True,
                 "10월 정기 라운딩은 레이크사이드 CC에서 진행합니다.\n"
                 "· 일시: 10월 11일(토) 07:00 첫 티오프\n· 그린피 15만원 / 캐디피 3.5만원 (팀당)\n"
                 "· 참가 신청은 모임 페이지에서 10월 8일까지 해주세요."),
                ("동호회 매너 규칙 재안내", False,
                 "· 티오프 30분 전 도착\n· 벙커 정리, 디봇 복구는 기본\n· 슬로우 플레이 주의\n"
                 "· 게스트 동반 시 사전 공지 필수\n즐거운 라운딩을 위해 협조 부탁드립니다."),
                ("3분기 회비 정산 완료", False,
                 "3분기 회비 정산이 완료되었습니다. 미납 회원은 개별 연락드리겠습니다.\n"
                 "정산 내역은 클럽 회비 메뉴에서 확인할 수 있습니다."),
            ]
            for title, important, content in notices:
                db.add(ClubNotice(club_id=club.id, title=title, content=content,
                                  is_important=important, is_private=False, author_id=leader.id))
            log.info("공지 %d건", len(notices))

        # ── 모임 ───────────────────────────────────────────────────────────
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        core = [leader] + members[:7] + ([reviewer] if reviewer else [])
        everyone = [leader] + members + ([reviewer] if reviewer else [])

        upcoming1 = add_round(db, club, leader, core,
                              name="10월 정기 라운딩", course=GOLF_COURSES[0],
                              description="이번 달 정기 라운딩입니다. 초보자 환영! 라운딩 후 점심 식사 함께합니다.",
                              when=(now + timedelta(days=12)).replace(hour=7), status="SCHEDULED")
        upcoming2 = add_round(db, club, leader, members[3:11],
                              name="번개 라운딩 — 스카이72", course=GOLF_COURSES[1],
                              description="주중 번개 라운딩. 선착순 8명.",
                              when=(now + timedelta(days=5)).replace(hour=8), status="SCHEDULED",
                              max_participants=8)
        done1 = add_round(db, club, leader, everyone[:12],
                          name="9월 정기 라운딩", course=GOLF_COURSES[2],
                          description="9월 정기 라운딩 (완료). 스코어와 정산 내역을 확인하세요.",
                          when=(now - timedelta(days=16)).replace(hour=7), status="COMPLETED")
        done2 = add_round(db, club, leader, core,
                          name="8월 정기 라운딩", course=GOLF_COURSES[3],
                          description="8월 정기 라운딩 (완료).",
                          when=(now - timedelta(days=44)).replace(hour=7), status="COMPLETED")
        done3 = add_round(db, club, leader, everyone[2:10] + [leader],
                          name="7월 정기 라운딩", course=GOLF_COURSES[4],
                          description="7월 정기 라운딩 (완료).",
                          when=(now - timedelta(days=72)).replace(hour=7), status="COMPLETED")

        add_social(db, club, leader, everyone[:10],
                   name="10월 라운딩 뒤풀이", venue="강남 한우명가", location="서울 강남구",
                   description="정기 라운딩 후 저녁 식사 모임입니다. 라운딩 불참자도 환영!",
                   when=(now + timedelta(days=12)).replace(hour=18), status="SCHEDULED",
                   social_type=SocialType.DINNER, cost=45000)
        add_social(db, club, leader, everyone[:9],
                   name="9월 스크린골프 번개", venue="골프존파크 잠실점", location="서울 송파구",
                   description="비 오는 날 스크린으로 대체한 번개 모임 (완료).",
                   when=(now - timedelta(days=9)).replace(hour=19), status="COMPLETED",
                   social_type=SocialType.CASUAL, cost=25000)
        db.flush()

        # ── 스코어 ─────────────────────────────────────────────────────────
        n = 0
        for m, ps in ((done1, everyone[:12]), (done2, core), (done3, everyone[2:10] + [leader])):
            n += record_scores(db, m, ps, rng)
        log.info("스코어 기록 %d건", n)

        db.commit()
        log.info("완료. 클럽 #%s '%s' — 클럽장 %s", club.id, club.name, leader.email)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
